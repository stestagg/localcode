import tempfile
from pathlib import Path
from unittest import IsolatedAsyncioTestCase

from localcode.project import Project, Runtime
from localcode.server.app import create_app
from localcode.server.ws import Clients, COMMANDS, pack_message, unpack_message


class WebsocketProtocolTest(IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        async def echo(command):
            await command.send({"type": "echo", "payload": command.data["payload"]})

        self.previous_echo = COMMANDS.get("test.echo")
        COMMANDS["test.echo"] = echo

    async def asyncTearDown(self) -> None:
        if self.previous_echo is None:
            COMMANDS.pop("test.echo", None)
        else:
            COMMANDS["test.echo"] = self.previous_echo

    async def test_binary_payload_round_trips_as_messagepack(self) -> None:
        project = Project(Path("."), Runtime(secret="test-secret"))
        client = create_app(project, Clients()).test_client()

        async with client.websocket("/ws") as socket:
            await socket.send(pack_message({"action": "auth", "secret": "test-secret"}))
            self.assertEqual(unpack_message(await socket.receive()), {"type": "ready"})

            payload = b"\x00\x80\xff"
            await socket.send(
                pack_message({"id": "client-run-id", "action": "test.echo", "payload": payload})
            )
            response = unpack_message(await socket.receive())

        self.assertEqual(response["type"], "echo")
        self.assertEqual(response["id"], "client-run-id")
        self.assertEqual(response["action"], "test.echo")
        self.assertEqual(response["payload"], payload)

    async def test_stories_returns_only_requested_states_in_number_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "demo"
            localcode = root / ".localcode"
            backlog = localcode / "stories/backlog"
            done = localcode / "stories/done"
            backlog.mkdir(parents=True)
            done.mkdir(parents=True)
            (backlog / "10-later.md").write_text("Later.\n")
            (backlog / "02-first.md").write_text("---\ntitle: First\n---\n")
            (done / "01-archived.md").write_text("Archived.\n")
            project = Project(root, Runtime(secret="test-secret", http_port=8080))
            client = create_app(project, Clients()).test_client()

            async with client.websocket("/ws") as socket:
                await socket.send(pack_message({"action": "auth", "secret": "test-secret"}))
                self.assertEqual(unpack_message(await socket.receive()), {"type": "ready"})
                await socket.send(
                    pack_message({"action": "stories.list", "states": ["backlog"]})
                )
                response = unpack_message(await socket.receive())

            self.assertEqual(list(response["states"]), ["backlog"])
            self.assertEqual(
                [story["number"] for story in response["states"]["backlog"]],
                [2, 10],
            )
            self.assertEqual(
                response["states"]["backlog"][0]["path"],
                ".localcode/stories/backlog/02-first.md",
            )

    def test_text_frames_are_rejected(self) -> None:
        with self.assertRaisesRegex(TypeError, "binary MessagePack"):
            unpack_message('{"action":"status"}')  # type: ignore[arg-type]
