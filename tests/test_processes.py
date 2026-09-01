import json
from pathlib import Path
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, patch

from localcode.docker import process
from localcode.driver import processes
from localcode.project import Runtime
from tests.support import make_project


async def discard(_: dict) -> None:
    pass


class ProcessRegistryTest(TestCase):
    def test_only_trusted_builtins_resolve(self) -> None:
        self.assertIs(processes.get("chat"), processes.CHAT)
        self.assertIs(processes.get("story"), processes.STORY)
        self.assertEqual(
            [item["name"] for item in processes.catalog()], ["chat", "story"]
        )
        self.assertTrue(processes.catalog()[0]["interactive"])
        self.assertTrue(processes.catalog()[1]["requiresStory"])
        with self.assertRaisesRegex(processes.UnknownProcess, "process is required"):
            processes.get("")
        with self.assertRaisesRegex(processes.UnknownProcess, "no process 'other'"):
            processes.get("other")

    def test_definitions_reject_untrusted_names_and_script_paths(self) -> None:
        values = {
            "name": "valid",
            "title": "Valid",
            "description": "A process.",
            "script": "/opt/localcode/processes/valid.ts",
        }
        with self.assertRaisesRegex(ValueError, "invalid process name"):
            processes.ProcessDefinition(**{**values, "name": "../escape"})
        with self.assertRaisesRegex(ValueError, "directly under"):
            processes.ProcessDefinition(**{**values, "script": "/tmp/valid.ts"})

    def test_registry_names_are_unique(self) -> None:
        with self.assertRaisesRegex(ValueError, "must be unique"):
            processes._index((processes.CHAT, processes.CHAT))

    def test_story_process_roles_are_in_the_project_template(self) -> None:
        roles = Path("src/localcode/templates/localcode/roles")
        for name in processes.STORY.roles:
            with self.subTest(role=name):
                self.assertTrue((roles / f"{name}.md").read_text().strip())


class ProcessContainerTest(IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.project = make_project(
            self,
            personas={"alice": "Be incisive.\n"},
            roles={
                "story-pre-dev": "Refine it.\n",
                "story-developer": "Build it.\n",
            },
            runtime=Runtime(
                secret="secret",
                ws_port=9000,
                personas={"alice": "alice-token"},
            ),
        )

    async def test_chat_gets_a_clone_session_persona_and_direct_model_config(self) -> None:
        with (
            patch.object(process, "ensure_image", AsyncMock()) as ensure,
            patch.object(process, "AgentContainer") as container_type,
            patch.object(process.llm_container, "environment", return_value={"MODEL": "yes"}),
        ):
            container_type.return_value.stream = AsyncMock(return_value=0)
            code = await process.run(
                self.project, processes.CHAT, "alice", "session-1", discard
            )

        self.assertEqual(code, 0)
        ensure.assert_awaited_once_with(discard)
        options = container_type.call_args.kwargs
        self.assertEqual(options["name"], self.project.process_container("session-1"))
        self.assertEqual(options["entrypoint"], "/process-entrypoint.sh")
        self.assertEqual(options["command"], [processes.CHAT.script])
        self.assertEqual(options["env"]["LOCALCODE_PERSONA_PROMPT"], "Be incisive.")
        self.assertEqual(options["env"]["LOCALCODE_SESSION"], "session-1")
        self.assertEqual(options["env"]["MODEL"], "yes")
        self.assertNotIn("LOCALCODE_ROLE", options["env"])

    async def test_story_gets_both_roles_path_branch_and_gitea_identity(self) -> None:
        with (
            patch.object(process, "ensure_image", AsyncMock()),
            patch.object(process, "AgentContainer") as container_type,
        ):
            container_type.return_value.stream = AsyncMock(return_value=0)
            code = await process.run(
                self.project,
                processes.STORY,
                "alice",
                "session-1",
                discard,
                story=".localcode/stories/ready/01-demo.md",
            )

        self.assertEqual(code, 0)
        options = container_type.call_args.kwargs
        env = options["env"]
        self.assertEqual(options["command"], [processes.STORY.script])
        self.assertEqual(env["LOCALCODE_STORY_PATH"], ".localcode/stories/ready/01-demo.md")
        self.assertEqual(env["LOCALCODE_BRANCH"], "process/alice/story-session-1")
        self.assertEqual(
            json.loads(env["LOCALCODE_ROLE_PROMPTS"]),
            {"story-pre-dev": "Refine it.", "story-developer": "Build it."},
        )

    async def test_an_unprovisioned_persona_is_rejected(self) -> None:
        self.project.runtime.personas.clear()
        with self.assertRaisesRegex(process.UnknownPersona, "has not been provisioned"):
            await process.run(
                self.project, processes.CHAT, "alice", "session-1", discard
            )

    async def test_story_requires_its_process_roles(self) -> None:
        (self.project.roles_dir / "story-pre-dev.md").unlink()
        with self.assertRaisesRegex(process.UnknownRole, "requires role 'story-pre-dev'"):
            await process.run(
                self.project,
                processes.STORY,
                "alice",
                "session-1",
                discard,
                story=".localcode/stories/ready/01-demo.md",
            )
