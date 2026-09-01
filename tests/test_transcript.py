"""Process logs: a record of what a container said, beside the live view.

The wrapper's whole job is to be invisible -- everything still reaches whoever
was listening, and the file is a bonus. So most of what is worth checking here
is that it does not get in the way when it cannot do its job.
"""

import tempfile
from pathlib import Path
from unittest import IsolatedAsyncioTestCase

from localcode.driver.process import run_command
from localcode.driver.transcript import render, transcript
from localcode.project import Project, Runtime


class RenderTest(IsolatedAsyncioTestCase):
    def test_a_command_line_is_quoted_the_way_a_shell_would_take_it(self) -> None:
        """The line is there to be pasted back, so it has to survive that."""
        self.assertEqual(
            render({"type": "start", "argv": ["docker", "run", "-e", "P=a b"]}),
            "$ docker run -e 'P=a b'\n",
        )

    def test_output_is_verbatim_because_chunks_are_not_lines(self) -> None:
        self.assertEqual(render({"type": "stdout", "data": "half a li"}), "half a li")
        self.assertEqual(render({"type": "stderr", "data": "ne\n"}), "ne\n")

    def test_how_it_ended_is_said_either_way(self) -> None:
        self.assertEqual(render({"type": "exit", "code": 2}), "exit 2\n")
        self.assertEqual(render({"type": "error", "message": "no docker"}), "error: no docker\n")

    def test_anything_else_is_not_the_transcript_s_business(self) -> None:
        self.assertEqual(render({"type": "metadata.changed"}), "")


class TranscriptTest(IsolatedAsyncioTestCase):
    async def test_a_real_run_reads_back_as_what_happened(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "logs" / "run.log"
            seen: list[dict] = []

            async with transcript(path, _collect(seen)) as events:
                code = await run_command(
                    ["sh", "-c", "echo out; echo err >&2; exit 3"], events
                )

            self.assertEqual(code, 3)
            written = path.read_text()
            self.assertIn("$ sh -c 'echo out; echo err >&2; exit 3'\n", written)
            self.assertIn("out\n", written)
            self.assertIn("err\n", written)
            self.assertTrue(written.endswith("exit 3\n"))
            # And the live view saw exactly the same events it always did.
            self.assertEqual(seen[0]["type"], "start")
            self.assertEqual(seen[-1], {"type": "exit", "code": 3})
            self.assertIn({"type": "stderr", "data": "err\n"}, seen)

    async def test_the_log_is_private_from_the_moment_it_exists(self) -> None:
        """A docker argv carries the agent's gitea token and the provider key."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run.log"
            async with transcript(path, _collect([])) as events:
                await events({"type": "start", "argv": ["docker"]})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    async def test_a_second_run_appends_rather_than_replacing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run.log"
            for code in (0, 1):
                async with transcript(path, _collect([])) as events:
                    await events({"type": "exit", "code": code})
            self.assertEqual(path.read_text(), "exit 0\nexit 1\n")

    async def test_nowhere_to_write_is_not_a_reason_not_to_run(self) -> None:
        """The log is a record of the work, not the work."""
        with tempfile.TemporaryDirectory() as directory:
            # A file where the directory would have to be.
            blocked = Path(directory) / "wall"
            blocked.write_text("")
            seen: list[dict] = []

            async with transcript(blocked / "run.log", _collect(seen)) as events:
                code = await run_command(["sh", "-c", "echo fine"], events)

            self.assertEqual(code, 0)
            self.assertIn({"type": "stdout", "data": "fine\n"}, seen)


class NamingTest(IsolatedAsyncioTestCase):
    def test_a_log_is_named_so_ls_sorts_it_and_a_run_can_be_found(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Project(Path(directory) / "demo", Runtime())
            path = project.process_log("agent.run", "a1b2c3d4")

            self.assertEqual(path.parent, project.state_dir / "process_logs")
            self.assertTrue(path.name.endswith("-agent.run-a1b2c3d4.log"))
            # Dots are fine in a filename; a slash in an action name is not.
            self.assertNotIn("/", project.process_log("a/b", "c/d").name)


def _collect(seen: list[dict]):
    async def stream(event: dict) -> None:
        seen.append(event)

    return stream
