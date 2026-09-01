from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from localcode.docker import agent
from localcode.project import Runtime
from tests.support import make_project


async def discard(_: dict) -> None:
    pass


class AgentRunTest(IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.project = make_project(
            self,
            personas={"alice": "Be incisive.\n", "bob": "Be pragmatic.\n"},
            roles={
                "story-reviewer": "Review the selected story.\n",
                "story-developer": "Develop the selected story.\n",
            },
            runtime=Runtime(
                http_port=8080,
                personas={"alice": "alice-token", "bob": "bob-token"},
            ),
        )

    async def test_composes_any_persona_and_role_into_one_container(self) -> None:
        fake_uuid = type("Uuid", (), {"hex": "12345678abcdef"})()
        with (
            patch.object(agent, "ensure_image", AsyncMock()) as ensure,
            patch.object(agent, "uuid4", return_value=fake_uuid),
            patch.object(agent, "AgentContainer") as container_type,
        ):
            container_type.return_value.stream = AsyncMock(return_value=0)
            code = await agent.run(
                self.project,
                "alice",
                "story-reviewer",
                "opencode",
                discard,
            )

        self.assertEqual(code, 0)
        ensure.assert_awaited_once_with(discard)
        options = container_type.call_args.kwargs
        self.assertEqual(
            options["labels"],
            {
                "localcode.persona": "alice",
                "localcode.role": "story-reviewer",
                "localcode.runner": "opencode",
            },
        )
        self.assertEqual(options["env"]["LOCALCODE_USER"], "alice")
        self.assertEqual(options["env"]["LOCALCODE_AGENT"], "alice")
        self.assertEqual(options["env"]["LOCALCODE_PERSONA"], "alice")
        self.assertEqual(options["env"]["LOCALCODE_ROLE"], "story-reviewer")
        self.assertEqual(options["env"]["LOCALCODE_PERSONA_PROMPT"], "Be incisive.")
        self.assertEqual(
            options["env"]["LOCALCODE_ROLE_PROMPT"], "Review the selected story."
        )
        self.assertEqual(options["env"]["LOCALCODE_TOKEN"], "alice-token")
        self.assertEqual(
            options["env"]["LOCALCODE_BRANCH"],
            "agent/alice/story-reviewer-opencode-12345678",
        )

    async def test_rejects_persona_and_role_independently(self) -> None:
        with self.assertRaisesRegex(agent.UnknownPersona, "no persona 'missing'"):
            await agent.run(
                self.project, "missing", "story-developer", "hello", discard
            )

        with self.assertRaisesRegex(agent.UnknownRole, "no role 'missing'"):
            await agent.run(self.project, "bob", "missing", "hello", discard)

    async def test_rejects_an_unprovisioned_persona(self) -> None:
        self.project.runtime.personas.pop("alice")

        with self.assertRaisesRegex(agent.UnknownPersona, "has not been provisioned"):
            await agent.run(
                self.project, "alice", "story-developer", "hello", discard
            )
