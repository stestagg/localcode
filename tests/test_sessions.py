"""Sessions: the record, the fan-out, and the control channel.

What is worth pinning here is the part that is not obvious from reading the
handlers: that a tab which arrives late still sees everything, that a streamed
answer costs the record one line rather than one per token, and that a session
outlives the process that made it.
"""

import asyncio
import json
import tempfile
from pathlib import Path
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import patch

from localcode.driver import processes
from localcode.project import Project, Runtime
from localcode.server import sessions as store
from localcode.server.app import create_app
from localcode.server.sessions import Sessions, UnknownSession
from localcode.server.ws import Clients, pack_message, unpack_message


class RecordingSocket:
    """Anything with an async send(bytes) satisfies the fan-out."""

    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def send(self, frame: bytes) -> None:
        self.messages.append(unpack_message(frame))

    def types(self) -> list[str]:
        return [message["type"] for message in self.messages]


class SessionCase(TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        root = Path(self._directory.name) / "demo"
        (root / ".localcode").mkdir(parents=True)
        self.project = Project(root, Runtime(secret="test-secret"))
        self.sessions = Sessions(self.project)

    def tearDown(self) -> None:
        self._directory.cleanup()

    def lines(self, session) -> list[dict]:
        return [json.loads(line) for line in session.path.read_text().splitlines() if line]


class TheRecordTest(SessionCase):
    def test_a_new_session_opens_its_file_with_a_header(self) -> None:
        session = self.sessions.create("s-1", title="ask developer", agent="developer")

        self.assertEqual(session.path, self.project.sessions_dir / "s-1.jsonl")
        self.assertEqual(
            self.lines(session),
            [
                {
                    "kind": "meta",
                    "id": "s-1",
                    "title": "ask developer",
                    "agent": "developer",
                    "process": "ask",
                    "at": session.at,
                }
            ],
        )

    def test_the_record_is_private_from_the_moment_it_exists(self) -> None:
        """As private as the work that produced it, and as runtime.json is."""
        session = self.sessions.create("s-1", title="t")
        self.assertEqual(session.path.stat().st_mode & 0o777, 0o600)

    def test_a_streamed_answer_is_one_line_not_one_per_chunk(self) -> None:
        session = self.sessions.create("s-1", title="t")
        session.post("dev", store.TEXT, text="Three ", stream="a", done=False)
        session.post("dev", store.TEXT, text="things ", stream="a", done=False)

        # Nothing written while it is still arriving...
        self.assertEqual([line.get("kind") for line in self.lines(session)], ["meta"])

        session.post("dev", store.TEXT, text="stand out.", stream="a", done=True)

        written = [line for line in self.lines(session) if "seq" in line]
        self.assertEqual(len(written), 1)
        self.assertEqual(written[0]["text"], "Three things stand out.")
        # And in memory it was one growing message all along, so a tab watching
        # saw it appear rather than arrive at the end.
        self.assertEqual([message.text for message in session.messages],
                         ["Three things stand out."])

    def test_seq_stays_this_sides_to_assign(self) -> None:
        """The author names its stream; the numbering is not up to it."""
        session = self.sessions.create("s-1", title="t")
        session.post("you", store.TEXT, text="q")
        session.post("dev", store.TEXT, text="a", stream="anything-at-all", done=True)
        session.post("dev", store.TEXT, text="b")

        self.assertEqual([message.seq for message in session.messages], [0, 1, 2])

    def test_flush_keeps_a_partial_answer(self) -> None:
        """A run that was stopped keeps whatever the model had said."""
        session = self.sessions.create("s-1", title="t")
        session.post("dev", store.TEXT, text="half an ans", stream="a", done=False)

        flushed = session.flush()

        self.assertEqual([message.text for message in flushed], ["half an ans"])
        self.assertEqual(
            [line["text"] for line in self.lines(session) if "seq" in line],
            ["half an ans"],
        )
        self.assertEqual(session.flush(), [])

    def test_a_state_change_is_appended_rather_than_rewritten(self) -> None:
        session = self.sessions.create("s-1", title="t")
        session.set_state(store.PAUSED)
        session.set_state(store.PAUSED)  # no change, nothing written
        session.set_state(store.STOPPED)

        self.assertEqual(
            [line["state"] for line in self.lines(session) if line.get("kind") == "state"],
            ["paused", "stopped"],
        )

    def test_an_image_survives_the_round_trip_through_base64(self) -> None:
        session = self.sessions.create("s-1", title="t")
        blob = bytes(range(256))
        session.post("dev", store.IMAGE, mime="image/png", data=blob)

        reloaded = store.Session.load(session.path)
        self.assertIsNotNone(reloaded)
        self.assertEqual(reloaded.messages[0].data, blob)
        self.assertEqual(reloaded.messages[0].mime, "image/png")
        # And on the wire it is bytes, which is the whole reason for MessagePack.
        self.assertIsInstance(session.messages[0].wire()["data"], bytes)


class ReloadingTest(SessionCase):
    def test_a_session_outlives_the_process_that_made_it(self) -> None:
        session = self.sessions.create("s-1", title="ask developer", agent="developer")
        session.post("you", store.TEXT, text="why?")
        session.post("developer", store.TEXT, text="because.")

        # A new controller, which has never heard of it.
        restarted = Sessions(self.project).get("s-1")

        self.assertEqual(restarted.title, "ask developer")
        self.assertEqual(restarted.agent, "developer")
        self.assertEqual(restarted.process, "ask")
        self.assertEqual(
            [(message.agent, message.text) for message in restarted.messages],
            [("you", "why?"), ("developer", "because.")],
        )
        # It was still running when the record stopped growing, so nobody ever
        # saw how it ended. That is `abandoned`, and specifically not
        # `finished`: "it worked" is a claim, and loading a file is not
        # evidence for it.
        self.assertEqual(restarted.state, store.ABANDONED)
        # Numbering carries on rather than starting again.
        self.assertEqual(restarted.post("developer", store.TEXT, text="more").seq, 2)

    def test_a_finished_session_keeps_saying_so(self) -> None:
        """The one state that has to survive a reload, because it was earned."""
        session = self.sessions.create("s-1", title="t")
        session.set_state(store.FINISHED)
        self.assertEqual(Sessions(self.project).get("s-1").state, store.FINISHED)

    def test_process_metadata_survives_and_legacy_records_are_ask(self) -> None:
        chat = self.sessions.create("chat", title="chat dev", process="chat")
        chat.set_state(store.FINISHED)
        self.assertEqual(Sessions(self.project).get("chat").process, "chat")

        legacy = self.sessions.create("legacy", title="ask dev")
        records = self.lines(legacy)
        records[0].pop("process")
        legacy.path.write_text("\n".join(json.dumps(item) for item in records) + "\n")
        self.sessions._live.pop("legacy")
        self.assertEqual(self.sessions.get("legacy").process, "ask")

    def test_a_stopped_session_is_still_stopped_when_it_comes_back(self) -> None:
        session = self.sessions.create("s-1", title="t")
        session.set_state(store.STOPPED)
        self.assertEqual(Sessions(self.project).get("s-1").state, store.STOPPED)

    def test_a_half_written_line_costs_that_line_and_no_more(self) -> None:
        """The controller was killed mid-append. The rest is still worth having."""
        session = self.sessions.create("s-1", title="t")
        session.post("you", store.TEXT, text="kept")
        with session.path.open("a") as handle:
            handle.write('{"seq": 1, "text": "trunc')

        reloaded = Sessions(self.project).get("s-1")
        self.assertEqual([message.text for message in reloaded.messages], ["kept"])

    def test_an_unknown_session_is_unknown_rather_than_empty(self) -> None:
        with self.assertRaises(UnknownSession):
            self.sessions.get("s-nope")

    def test_an_id_that_would_escape_the_directory_is_refused(self) -> None:
        with self.assertRaises(UnknownSession):
            self.sessions.get("../../etc/passwd")
        with self.assertRaises(ValueError):
            self.sessions.create("../escape", title="t")

    def test_a_duplicate_id_is_refused_rather_than_silently_joined(self) -> None:
        self.sessions.create("s-1", title="t")
        with self.assertRaises(store.SessionExists):
            self.sessions.create("s-1", title="t")

    def test_listing_finds_sessions_this_process_never_had(self) -> None:
        first = self.sessions.create("s-1", title="first")
        second = self.sessions.create("s-2", title="second")
        first.at, second.at = 1.0, 2.0

        listed = Sessions(self.project).list()

        self.assertEqual([item["title"] for item in listed], ["second", "first"])
        self.assertEqual(listed[0]["messages"], 0)


class FanOutTest(IsolatedAsyncioTestCase, SessionCase):
    async def test_viewers_and_workers_are_told_different_things(self) -> None:
        session = self.sessions.create("s-1", title="t")
        viewer, worker = RecordingSocket(), RecordingSocket()
        session.viewers.add(viewer)
        session.workers.add(worker)

        await session.broadcast({"type": "session.message"})
        await session.signal({"type": "session.input", "text": "hi"})

        self.assertEqual(viewer.types(), ["session.message"])
        self.assertEqual(worker.types(), ["session.input"])
        # Every event says which session it is about: one socket can be
        # watching several.
        self.assertEqual(viewer.messages[0]["session"], "s-1")

    async def test_a_socket_that_went_away_is_forgotten_everywhere(self) -> None:
        first = self.sessions.create("s-1", title="t")
        second = self.sessions.create("s-2", title="t")
        socket = RecordingSocket()
        first.viewers.add(socket)
        second.workers.add(socket)

        self.sessions.discard(socket)

        await first.broadcast({"type": "session.message"})
        await second.signal({"type": "session.state"})
        self.assertEqual(socket.messages, [])


class ProtocolTest(IsolatedAsyncioTestCase):
    """The handlers, over a real socket, the way `test_ws.py` does it."""

    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        root = Path(self._directory.name) / "demo"
        (root / ".localcode").mkdir(parents=True)
        self.project = Project(root, Runtime(secret="test-secret"))
        self.sessions = Sessions(self.project)
        self.client = create_app(
            self.project, Clients(), sessions=self.sessions
        ).test_client()

    def tearDown(self) -> None:
        self._directory.cleanup()

    async def opened(self, socket) -> None:
        await socket.send(pack_message({"action": "auth", "secret": "test-secret"}))
        self.assertEqual(unpack_message(await socket.receive()), {"type": "ready"})

    async def test_subscribing_replays_what_was_said_before_it_arrived(self) -> None:
        """The whole reason a client gets to name its own session: nothing that
        happens between asking and subscribing can be missed."""
        session = self.sessions.create("s-1", title="ask developer", agent="developer")
        session.post("you", store.TEXT, text="why?")
        session.post("developer", store.TEXT, text="because.")

        async with self.client.websocket("/ws") as socket:
            await self.opened(socket)
            await socket.send(pack_message({"action": "session.subscribe", "session": "s-1"}))
            history = unpack_message(await socket.receive())

        self.assertEqual(history["type"], "session.history")
        self.assertEqual(history["title"], "ask developer")
        self.assertEqual([message["text"] for message in history["messages"]],
                         ["why?", "because."])

    async def test_control_moves_the_state_and_says_so_both_ways(self) -> None:
        session = self.sessions.create("s-1", title="t")
        worker = RecordingSocket()
        session.workers.add(worker)

        async with self.client.websocket("/ws") as socket:
            await self.opened(socket)
            await socket.send(pack_message({"action": "session.subscribe", "session": "s-1"}))
            await socket.receive()  # the history
            await socket.send(
                pack_message({"action": "session.control", "session": "s-1", "control": "pause"})
            )
            paused = unpack_message(await socket.receive())

        self.assertEqual(paused, {"type": "session.state", "state": "paused", "session": "s-1"})
        self.assertEqual(session.state, store.PAUSED)
        # The agent hears it too, or a cooperative pause never happens.
        self.assertEqual(worker.messages, [paused])

    async def test_an_unknown_control_is_refused(self) -> None:
        self.sessions.create("s-1", title="t")
        async with self.client.websocket("/ws") as socket:
            await self.opened(socket)
            await socket.send(
                pack_message({"action": "session.control", "session": "s-1", "control": "melt"})
            )
            response = unpack_message(await socket.receive())

        self.assertEqual(response["type"], "error")
        self.assertIn("pause, resume, stop", response["message"])
        self.assertEqual(self.sessions.get("s-1").state, store.RUNNING)

    async def test_typed_input_is_both_a_message_and_an_instruction(self) -> None:
        session = self.sessions.create("s-1", title="t")
        worker = RecordingSocket()
        session.workers.add(worker)

        async with self.client.websocket("/ws") as socket:
            await self.opened(socket)
            await socket.send(pack_message({"action": "session.subscribe", "session": "s-1"}))
            await socket.receive()
            await socket.send(
                pack_message({"action": "session.input", "session": "s-1", "text": "try again"})
            )
            broadcast = unpack_message(await socket.receive())

        # In the transcript, attributed to the person...
        self.assertEqual(broadcast["post"]["agent"], store.USER)
        self.assertEqual(broadcast["post"]["text"], "try again")
        # ...and queued for the agent to find at its next checkpoint.
        self.assertEqual([message.text for message in session.inputs], ["try again"])
        self.assertEqual(worker.messages[-1]["input"]["text"], "try again")
        self.assertEqual(worker.messages[-1]["input"]["seq"], broadcast["post"]["seq"])

    async def test_an_empty_input_is_not_a_message(self) -> None:
        self.sessions.create("s-1", title="t")
        async with self.client.websocket("/ws") as socket:
            await self.opened(socket)
            await socket.send(
                pack_message({"action": "session.input", "session": "s-1", "text": "   "})
            )
            response = unpack_message(await socket.receive())

        self.assertEqual(response["type"], "error")
        self.assertEqual(self.sessions.get("s-1").messages, [])

    async def test_attaching_says_where_things_stand_straight_away(self) -> None:
        """An agent that attached after a stop has to find out, not work on."""
        session = self.sessions.create("s-1", title="t")
        session.set_state(store.STOPPED)

        async with self.client.websocket("/ws") as socket:
            await self.opened(socket)
            await socket.send(pack_message({"action": "session.attach", "session": "s-1"}))
            response = unpack_message(await socket.receive())

        self.assertEqual(response, {"type": "session.state", "session": "s-1", "state": "stopped"})

    async def test_posting_reaches_viewers_as_it_streams(self) -> None:
        session = self.sessions.create("s-1", title="t")
        viewer = RecordingSocket()
        session.viewers.add(viewer)

        async with self.client.websocket("/ws") as socket:
            await self.opened(socket)
            for text, done in (("Three ", False), ("things", True)):
                await socket.send(
                    pack_message(
                        {
                            "action": "session.post",
                            "session": "s-1",
                            "agent": "developer",
                            "text": text,
                            "stream": "a",
                            "done": done,
                        }
                    )
                )
                self.assertEqual(unpack_message(await socket.receive())["type"], "session.posted")

        # Both chunks went out, under one seq, growing as they arrived.
        streamed = [message["post"] for message in viewer.messages]
        self.assertEqual([message["text"] for message in streamed], ["Three ", "Three things"])
        self.assertEqual({message["seq"] for message in streamed}, {0})

    async def test_collect_drains_what_was_typed(self) -> None:
        session = self.sessions.create("s-1", title="t")
        session.inputs.extend(
            [
                session.post(store.USER, store.TEXT, text="first"),
                session.post(store.USER, store.TEXT, text="second"),
            ]
        )

        async with self.client.websocket("/ws") as socket:
            await self.opened(socket)
            await socket.send(pack_message({"action": "session.collect", "session": "s-1"}))
            response = unpack_message(await socket.receive())

        self.assertEqual(
            [(item["seq"], item["text"]) for item in response["inputs"]],
            [(0, "first"), (1, "second")],
        )
        self.assertEqual(response["state"], "running")
        self.assertEqual(list(session.inputs), [])

    async def test_closing_writes_out_whatever_was_still_streaming(self) -> None:
        session = self.sessions.create("s-1", title="t")

        async with self.client.websocket("/ws") as socket:
            await self.opened(socket)
            await socket.send(pack_message({"action": "session.subscribe", "session": "s-1"}))
            await socket.receive()
            await socket.send(
                pack_message(
                    {
                        "action": "session.post",
                        "session": "s-1",
                        "text": "half",
                        "stream": "a",
                        "done": False,
                    }
                )
            )
            await socket.receive()  # session.posted
            await socket.receive()  # the broadcast
            await socket.send(pack_message({"action": "session.close", "session": "s-1"}))
            flushed = unpack_message(await socket.receive())
            closed = unpack_message(await socket.receive())

        self.assertEqual(flushed["post"]["text"], "half")
        self.assertEqual(closed, {"type": "session.closed", "state": "finished", "session": "s-1"})
        written = [
            json.loads(line)
            for line in session.path.read_text().splitlines()
            if line and "seq" in json.loads(line)
        ]
        self.assertEqual([line["text"] for line in written], ["half"])

    async def test_a_nonzero_worker_close_is_failed(self) -> None:
        session = self.sessions.create("s-1", title="t", process="chat")

        async with self.client.websocket("/ws") as socket:
            await self.opened(socket)
            await socket.send(
                pack_message({"action": "session.subscribe", "session": "s-1"})
            )
            await socket.receive()
            await socket.send(
                pack_message({"action": "session.close", "session": "s-1", "code": 2})
            )
            closed = unpack_message(await socket.receive())

        self.assertEqual(closed["state"], store.FAILED)
        self.assertEqual(session.state, store.FAILED)

    async def test_a_dead_socket_stops_being_a_viewer(self) -> None:
        session = self.sessions.create("s-1", title="t")

        async with self.client.websocket("/ws") as socket:
            await self.opened(socket)
            await socket.send(pack_message({"action": "session.subscribe", "session": "s-1"}))
            await socket.receive()

        # Leaving the block closes it; the handler's `finally` is what clears it.
        await session.broadcast({"type": "session.message"})
        self.assertEqual(len(session.viewers._sockets), 0)


class OutcomeTest(IsolatedAsyncioTestCase, SessionCase):
    """How a run ended, and who is entitled to say so.

    `finished` is the only state that claims the work succeeded, so it is the
    only one nothing is allowed to infer. Everything here is about a run that
    ended some other way not being labelled complete.
    """

    def context(self):
        from localcode.server.ws import Context

        return Context(project=self.project, clients=Clients(), sessions=self.sessions)

    async def answer(self, ask_into, *, cancel: bool = False):
        """Run `_answer` with `llm.ask_into` replaced, and return the session."""
        from localcode.server import commands
        from localcode.server.ws import WsCommand

        class Sink:
            async def send(self, frame): pass

        session = self.sessions.create("s-1", title="ask dev", agent="dev")
        command = WsCommand("ask", {}, Sink(), self.context(), id="r1")
        replaced, commands.llm.ask_into = commands.llm.ask_into, ask_into
        try:
            task = self.sessions.start(commands._answer(command, session, "why?", ""))
            if cancel:
                await asyncio.sleep(0.05)
                await self.sessions.shutdown()
            else:
                await task
        finally:
            commands.llm.ask_into = replaced
        return session

    async def process(self, run_container):
        """Run `_run_process` with its container replaced."""
        from localcode.server import commands
        from localcode.server.ws import WsCommand

        class Sink:
            async def send(self, frame): pass

        session = self.sessions.create(
            "process-1", title="chat dev", agent="dev", process="chat"
        )
        command = WsCommand(
            "process.start", {}, Sink(), self.context(), id="process-run"
        )
        with patch.object(commands.process_container, "run", new=run_container):
            await commands._run_process(
                command, session, processes.CHAT, "dev"
            )
        return session

    async def test_a_run_that_finished_says_so(self) -> None:
        async def ran(*args, **kwargs) -> int:
            return 0

        self.assertEqual((await self.answer(ran)).state, store.FINISHED)

    async def test_a_non_zero_exit_is_a_failure_not_a_completion(self) -> None:
        async def failed(*args, **kwargs) -> int:
            return 2

        self.assertEqual((await self.answer(failed)).state, store.FAILED)

    async def test_a_raised_error_is_reported_where_the_person_is_looking(self) -> None:
        """Nothing is awaiting this run, so an exception has nowhere else to go."""

        async def broke(*args, **kwargs) -> int:
            raise RuntimeError("no docker")

        session = await self.answer(broke)

        self.assertEqual(session.state, store.FAILED)
        self.assertEqual(
            [(message.kind, message.text) for message in session.messages],
            [(store.STATUS, "RuntimeError: no docker")],
        )

    async def test_a_cancelled_run_is_abandoned_rather_than_complete(self) -> None:
        """The bug this exists for: a `finally` cannot tell a good answer from
        a cancellation, so it must not be the thing that claims success."""

        async def forever(*args, **kwargs) -> int:
            await asyncio.sleep(3600)
            return 0

        session = await self.answer(forever, cancel=True)

        self.assertEqual(session.state, store.ABANDONED)
        self.assertNotEqual(session.state, store.FINISHED)

    async def test_a_stop_outranks_however_the_container_then_exited(self) -> None:
        """A person's decision is not overwritten by the exit code that follows."""

        async def stopped_midway(*args, **kwargs) -> int:
            self.sessions.get("s-1").set_state(store.STOPPED)
            return 0

        self.assertEqual((await self.answer(stopped_midway)).state, store.STOPPED)

    async def test_a_partial_answer_survives_whatever_ended_the_run(self) -> None:
        async def cut_short(*args, **kwargs) -> int:
            self.sessions.get("s-1").post("dev", store.TEXT, text="half an ans",
                                          stream="a", done=False)
            raise RuntimeError("cut off")

        session = await self.answer(cut_short)

        self.assertIn("half an ans", [message.text for message in session.messages])
        self.assertIn("half an ans", session.path.read_text())

    async def test_a_process_exit_code_settles_the_interactive_session(self) -> None:
        async def finished(*args, **kwargs) -> int:
            return 0

        async def failed(*args, **kwargs) -> int:
            return 7

        self.assertEqual((await self.process(finished)).state, store.FINISHED)
        self.sessions._live.pop("process-1")
        self.project.session_path("process-1").unlink()
        self.assertEqual((await self.process(failed)).state, store.FAILED)

    async def test_a_process_exception_is_visible_and_failed(self) -> None:
        async def broke(*args, **kwargs) -> int:
            raise RuntimeError("container vanished")

        session = await self.process(broke)

        self.assertEqual(session.state, store.FAILED)
        self.assertEqual(session.messages[-1].kind, store.STATUS)
        self.assertEqual(session.messages[-1].text, "RuntimeError: container vanished")

    async def test_human_stop_outranks_a_process_exit(self) -> None:
        async def stopped(*args, **kwargs) -> int:
            self.sessions.get("process-1").set_state(store.STOPPED)
            return 9

        self.assertEqual((await self.process(stopped)).state, store.STOPPED)


class OwnershipTest(IsolatedAsyncioTestCase, SessionCase):
    async def test_a_run_is_not_owned_by_the_socket_that_asked_for_it(self) -> None:
        """A tab that reloads mid-question used to cancel the run's cleanup out
        from under it, closing the session while its container carried on
        posting into it."""
        finished = asyncio.Event()

        async def work() -> None:
            await finished.wait()

        task = self.sessions.start(work())
        self.assertIn(task, self.sessions._running)

        # Whatever asked for it goes away; the work does not.
        self.sessions.discard(object())
        await asyncio.sleep(0.05)
        self.assertFalse(task.done())

        finished.set()
        await task
        self.assertNotIn(task, self.sessions._running)

    async def test_shutdown_stops_everything_still_going(self) -> None:
        async def forever() -> None:
            await asyncio.sleep(3600)

        task = self.sessions.start(forever())
        await self.sessions.shutdown()

        self.assertTrue(task.cancelled())
        self.assertEqual(self.sessions._running, set())
