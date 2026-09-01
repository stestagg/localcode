import asyncio
import tempfile
from pathlib import Path
from unittest import IsolatedAsyncioTestCase, TestCase

from localcode import repo
from localcode.project import Project
from localcode.server.watch import metadata_revision, watch_metadata
from localcode.server.ws import Clients, unpack_message


class MetadataRevisionTest(TestCase):
    def test_tracks_all_metadata_but_ignores_runtime_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            localcode = root / ".localcode"
            state = localcode / "state"
            stories = localcode / "stories/backlog"
            state.mkdir(parents=True)
            stories.mkdir(parents=True)
            (stories / "01-story.md").write_text("first\n")
            project = Project(root)

            initial = metadata_revision(project)
            (state / "runtime.json").write_text("changing runtime data\n")
            self.assertEqual(metadata_revision(project), initial)

            (stories / "01-story.md").write_text("updated\n")
            self.assertNotEqual(metadata_revision(project), initial)

    def test_tracks_main_when_head_remains_on_localcode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo.init(root)
            repo.git("config", "user.name", "Test", cwd=root)
            repo.git("config", "user.email", "test@example.com", cwd=root)
            repo.git("commit", "--allow-empty", "-m", "Initial", cwd=root)
            repo.git("branch", "localcode", cwd=root)
            repo.git("switch", "localcode", cwd=root)
            (root / ".localcode").mkdir()
            project = Project(root)
            initial = metadata_revision(project)

            first_main = repo.git("rev-parse", "main", cwd=root)
            tree = repo.git("rev-parse", "main^{tree}", cwd=root)
            second_main = repo.git(
                "commit-tree", tree, "-p", first_main, "-m", "Advance main", cwd=root
            )
            repo.git("update-ref", "refs/heads/main", second_main, cwd=root)

            self.assertNotEqual(metadata_revision(project), initial)


class RecordingSocket:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def send(self, frame: bytes) -> None:
        self.messages.append(unpack_message(frame))


async def _first_poll(polled: list[str], timeout: float = 5.0) -> None:
    """Wait for the watcher to have taken its baseline snapshot."""
    deadline = asyncio.get_running_loop().time() + timeout
    while not polled:
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("the watcher never took a baseline snapshot")
        await asyncio.sleep(0.01)


class MetadataWatcherTest(IsolatedAsyncioTestCase):
    async def test_broadcasts_generic_change_after_sync(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            personas = root / ".localcode/personas"
            personas.mkdir(parents=True)
            definition = personas / "developer.md"
            definition.write_text("first\n")
            project = Project(root)
            clients = Clients()
            socket = RecordingSocket()
            clients.add(socket)
            stop = asyncio.Event()
            synced: list[Project] = []

            async def sync(changed: Project) -> None:
                synced.append(changed)
                stop.set()

            polled: list[str] = []

            def remote_revision(_: Project) -> str:
                polled.append("")
                return ""

            task = asyncio.create_task(
                watch_metadata(
                    project,
                    clients,
                    stop,
                    sync,
                    interval=0.01,
                    remote_revision=remote_revision,
                )
            )
            # The watcher polls off the event loop, so the baseline snapshot is
            # not taken by the time create_task returns. Changing anything
            # before it lands would be absorbed into the baseline.
            await _first_poll(polled)
            definition.write_text("second\n")
            await asyncio.wait_for(task, timeout=5)

            self.assertEqual(synced, [project])
            self.assertEqual(socket.messages, [{"type": "metadata.changed"}])

    async def test_remote_metadata_advance_also_triggers_sync(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".localcode").mkdir()
            project = Project(root)
            clients = Clients()
            socket = RecordingSocket()
            clients.add(socket)
            stop = asyncio.Event()
            remote = ["first"]
            synced: list[Project] = []

            async def sync(changed: Project) -> None:
                synced.append(changed)
                stop.set()

            polled: list[str] = []

            def remote_revision(_: Project) -> str:
                # Read before recording, so a flip that follows the record can
                # never be the value this call returned.
                value = remote[0]
                polled.append(value)
                return value

            task = asyncio.create_task(
                watch_metadata(
                    project,
                    clients,
                    stop,
                    sync,
                    interval=0.01,
                    remote_revision=remote_revision,
                )
            )
            await _first_poll(polled)
            remote[0] = "second"
            await asyncio.wait_for(task, timeout=5)

            self.assertEqual(synced, [project])
            self.assertEqual(socket.messages, [{"type": "metadata.changed"}])
