from pathlib import Path
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, call, patch

from localcode.gitea import setup
from localcode.project import Persona, Project, Runtime


class PrimaryAccountTest(IsolatedAsyncioTestCase):
    async def test_creates_separate_automation_and_human_accounts(self) -> None:
        project = Project(Path("/project"), Runtime())
        gitea = AsyncMock()
        gitea.user_exists.side_effect = [False, False]

        with (
            patch.object(
                setup,
                "_create_account",
                AsyncMock(side_effect=["automation-password", "human-password"]),
            ) as create,
            patch.object(
                setup, "_mint_token", AsyncMock(return_value="automation-token")
            ) as mint,
        ):
            await setup._ensure_primary_accounts(project, gitea)

        self.assertEqual(
            create.await_args_list,
            [
                call(project, "localcode", admin=True),
                call(project, "human", admin=True),
            ],
        )
        mint.assert_awaited_once_with(project, "localcode")
        self.assertEqual(project.runtime.automation_token, "automation-token")
        self.assertEqual(project.runtime.human_password, "human-password")

    async def test_recovers_human_password_without_rotating_automation(self) -> None:
        project = Project(
            Path("/project"), Runtime(automation_token="existing-automation-token")
        )
        gitea = AsyncMock()
        gitea.user_exists.side_effect = [True, True]

        with (
            patch.object(setup, "_create_account", AsyncMock()) as create,
            patch.object(setup, "_mint_token", AsyncMock()) as mint,
            patch.object(
                setup, "_replace_password", AsyncMock(return_value="new-human-password")
            ) as replace,
        ):
            await setup._ensure_primary_accounts(project, gitea)

        create.assert_not_awaited()
        mint.assert_not_awaited()
        replace.assert_awaited_once_with(project, "human")
        self.assertEqual(project.runtime.human_password, "new-human-password")


class PersonaAccountTest(IsolatedAsyncioTestCase):
    async def test_syncs_gitea_accounts_and_tokens_from_personas(self) -> None:
        project = Project(
            Path("/project"), Runtime(personas={"alice": "existing-token"})
        )
        gitea = AsyncMock()
        gitea.user_exists.side_effect = [True, False]

        with (
            patch.object(Project, "personas", return_value=[
                Persona("alice", "First", Path("alice.md")),
                Persona("bob", "Second", Path("bob.md")),
            ]),
            patch.object(setup, "_create_account", AsyncMock()) as create,
            patch.object(
                setup, "_mint_token", AsyncMock(return_value="bob-token")
            ) as mint,
        ):
            added = await setup.sync_personas(project, gitea)

        create.assert_awaited_once_with(project, "bob")
        mint.assert_awaited_once_with(project, "bob")
        self.assertEqual(added, ["bob"])
        self.assertEqual(
            project.runtime.personas,
            {"alice": "existing-token", "bob": "bob-token"},
        )
        gitea.add_collaborator.assert_awaited_once_with("localcode", "project", "bob")
