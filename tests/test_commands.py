from contextlib import asynccontextmanager
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, Mock, patch

from localcode.docker import agent
from localcode.project import Project, Runtime
from localcode.server import commands
from localcode.server.sessions import Sessions
from localcode.server.ws import Clients, Context, WsCommand, unpack_message
from tests.support import make_project


class RecordingSocket:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def send(self, frame: bytes) -> None:
        self.messages.append(unpack_message(frame))


async def discard(_: dict) -> None:
    pass


class StubCommand:
    def __init__(self, project: Project, data: dict) -> None:
        self.project = project
        self.data = data
        self.published: list[dict] = []

    def broadcast_stream(self):
        return discard

    @asynccontextmanager
    async def recorded(self, stream):
        yield stream

    async def publish(self, event: dict) -> None:
        self.published.append(event)


class DefinitionCommandTest(IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.project = make_project(
            self,
            personas={"alice": "First line\nSecond line\n", "empty": ""},
            roles={
                "story-developer": "Develop a story.\n",
                "story-pre-dev": "Prepare a story.\n",
                "empty": "",
            },
            stories={"ready/01-demo.md": "Build it.\n"},
            runtime=Runtime(
                secret="secret",
                http_port=8080,
                automation_token="automation-token",
                personas={"alice": "alice-token"},
            ),
        )

    async def test_status_exposes_personas_roles_and_separate_issues(self) -> None:
        socket = RecordingSocket()
        command = WsCommand(
            action="status",
            data={},
            socket=socket,
            context=Context(self.project, Clients(), Sessions(self.project)),
            id="status-id",
        )
        with (
            patch.object(commands.git, "head_summary", return_value="head"),
            patch.object(commands.git, "branches", return_value=["main"]),
            patch.object(commands.git, "file_at_ref", return_value="# Demo"),
            patch.object(commands.client, "running", AsyncMock(return_value=True)),
        ):
            await commands.status(command)

        response = socket.messages[0]
        self.assertEqual([item["name"] for item in response["personas"]], ["alice"])
        self.assertEqual(
            [item["name"] for item in response["roles"]],
            ["story-developer", "story-pre-dev"],
        )
        self.assertEqual(response["personas"][0]["prompt"], "First line\nSecond line")
        self.assertIn(
            "/_edit/localcode/.localcode/personas/alice.md",
            response["personas"][0]["editUrl"],
        )
        self.assertIn(
            "/_edit/localcode/.localcode/roles/story-developer.md",
            response["roles"][0]["editUrl"],
        )
        self.assertEqual(
            [item["name"] for item in response["personaIssues"]], ["empty"]
        )
        self.assertEqual([item["name"] for item in response["roleIssues"]], ["empty"])
        self.assertEqual(response["processes"], commands.processes.catalog())
        self.assertIn(
            "/_edit/localcode/.localcode/personas/empty.md",
            response["personaIssues"][0]["editUrl"],
        )
        self.assertIn(
            "/_edit/localcode/.localcode/roles/empty.md",
            response["roleIssues"][0]["editUrl"],
        )
        self.assertNotIn("agents", response)
        self.assertNotIn("agentIssues", response)

    async def test_agent_run_requires_and_forwards_both_definitions(self) -> None:
        with self.assertRaisesRegex(agent.UnknownPersona, "persona is required"):
            await commands.agent_run(StubCommand(self.project, {"role": "story-developer"}))

        with self.assertRaisesRegex(agent.UnknownRole, "role is required"):
            await commands.agent_run(StubCommand(self.project, {"persona": "alice"}))

        command = StubCommand(
            self.project,
            {"persona": "alice", "role": "story-developer", "runner": "hello"},
        )
        with (
            patch.object(commands.agent, "run", AsyncMock(return_value=0)) as run,
            patch.object(commands.setup, "sync_metadata", AsyncMock()) as sync,
        ):
            await commands.agent_run(command)

        run.assert_awaited_once_with(
            self.project, "alice", "story-developer", "hello", discard
        )
        sync.assert_awaited_once_with(self.project)
        self.assertEqual(command.published, [{"type": "metadata.changed"}])

    async def test_personas_sync_uses_the_persona_catalog(self) -> None:
        class GiteaContext:
            async def __aenter__(self):
                return "gitea"

            async def __aexit__(self, *_):
                return None

        command = StubCommand(self.project, {})
        with (
            patch.object(commands, "Gitea", return_value=GiteaContext()),
            patch.object(
                commands.setup,
                "sync_personas",
                AsyncMock(return_value=["alice"]),
            ) as sync,
            patch.object(Project, "save_runtime") as save,
        ):
            await commands.personas_sync(command)

        sync.assert_awaited_once_with(self.project, "gitea")
        save.assert_called_once_with()
        self.assertEqual(command.published, [{"type": "personas", "added": ["alice"]}])

    async def test_ask_uses_only_the_selected_persona_prompt(self) -> None:
        message = Mock()
        message.wire.return_value = {"seq": 0}
        session = Mock(id="session-id", agent="alice", viewers=set())
        session.history.return_value = {"type": "session.history"}
        session.post.return_value = message
        session.broadcast = AsyncMock()
        session_store = Mock()
        session_store.create.return_value = session
        socket = RecordingSocket()

        class AskCommand:
            project = self.project
            data = {"session": "session-id", "persona": "alice", "prompt": "Why?"}
            sessions = session_store

        command = AskCommand()
        command.socket = socket
        with patch.object(commands, "_answer", return_value="background") as answer:
            await commands.ask(command)

        session_store.create.assert_called_once_with(
            "session-id", title="ask alice", agent="alice", process="ask"
        )
        answer.assert_called_once_with(command, session, "Why?", "First line\nSecond line")
        session_store.start.assert_called_once()
        session_store.start.call_args.args[0].close()

    async def test_process_start_creates_an_empty_interactive_session(self) -> None:
        session = Mock(id="chat-id", agent="alice", viewers=set())
        session.history.return_value = {"type": "session.history", "process": "chat"}
        session_store = Mock()
        session_store.create.return_value = session
        socket = RecordingSocket()

        class ProcessCommand:
            project = self.project
            data = {"session": "chat-id", "process": "chat", "persona": "alice"}
            sessions = session_store

        command = ProcessCommand()
        command.socket = socket
        with patch.object(commands, "_run_process", return_value="background") as run:
            await commands.process_start(command)

        session_store.create.assert_called_once_with(
            "chat-id", title="chat alice", agent="alice", process="chat"
        )
        self.assertEqual(session.container, self.project.process_container("chat-id"))
        self.assertEqual(socket.messages[0]["process"], "chat")
        run.assert_called_once()
        session_store.start.assert_called_once()
        session_store.start.call_args.args[0].close()

    async def test_process_start_rejects_unknown_processes_and_personas(self) -> None:
        class ProcessCommand:
            project = self.project
            sessions = Mock()
            socket = RecordingSocket()

        command = ProcessCommand()
        command.data = {"process": "missing", "persona": "alice"}
        with self.assertRaisesRegex(ValueError, "no process 'missing'"):
            await commands.process_start(command)

        command.data = {"process": "chat", "persona": "missing"}
        with self.assertRaisesRegex(agent.UnknownPersona, "no persona 'missing'"):
            await commands.process_start(command)

    async def test_story_process_requires_and_forwards_a_repo_story_path(self) -> None:
        session = Mock(id="story-id", agent="alice", viewers=set())
        session.history.return_value = {"type": "session.history", "process": "story"}
        session_store = Mock()
        session_store.create.return_value = session
        socket = RecordingSocket()

        class ProcessCommand:
            project = self.project
            data = {
                "session": "story-id",
                "process": "story",
                "persona": "alice",
                "story": ".localcode/stories/ready/01-demo.md",
            }
            sessions = session_store

        command = ProcessCommand()
        command.socket = socket
        with patch.object(commands, "_run_process", return_value="background") as run:
            await commands.process_start(command)

        session_store.create.assert_called_once_with(
            "story-id", title="implement story 01-demo.md", agent="alice", process="story"
        )
        run.assert_called_once_with(
            command,
            session,
            commands.processes.STORY,
            "alice",
            story=".localcode/stories/ready/01-demo.md",
        )
        session_store.start.call_args.args[0].close()

        command.data["story"] = "../README.md"
        with self.assertRaisesRegex(ValueError, "repo-relative path"):
            await commands.process_start(command)
