import json
import subprocess
import tempfile
from pathlib import Path
from unittest import TestCase

from localcode import repo
from localcode.llm import console
from localcode.project import NotAProject, Project, Runtime
from localcode.scaffold import scaffold


class LocalcodeBranchTest(TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.path = self.root / "repo"
        self.path.mkdir()
        repo.init(self.path)
        repo.git("config", "user.name", "Test", cwd=self.path)
        repo.git("config", "user.email", "test@example.com", cwd=self.path)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_scaffold_exists_only_on_localcode_branch(self) -> None:
        scaffold(self.path)

        self.assertEqual(repo.current_branch(self.path), "localcode")
        self.assertFalse((self.path / ".localcode/config.toml").exists())
        self.assertTrue((self.path / ".localcode/.gitignore").is_file())
        stories = self.path / ".localcode/stories"
        self.assertTrue((stories / "README.md").is_file())
        for stage in ("backlog", "ready", "in_progress", "done", "cancelled"):
            self.assertTrue((stories / stage).is_dir())
        for definitions in ("personas", "roles"):
            files = list((self.path / ".localcode" / definitions).glob("*.md"))
            self.assertTrue(files, f"scaffold has no {definitions}")
            self.assertTrue(
                all(path.read_text().strip() for path in files),
                f"scaffold has an empty {definitions} definition",
            )
        self.assertFalse((self.path / ".localcode/agents").exists())
        self.assertFalse((self.path / ".localcode/pm").exists())
        self.assertEqual(
            repo.git("ls-tree", "-r", "--name-only", "main", cwd=self.path), ""
        )
        self.assertIn(
            ".localcode/.gitignore",
            repo.git("ls-tree", "-r", "--name-only", "localcode", cwd=self.path),
        )

    def test_scaffold_removes_obsolete_config(self) -> None:
        scaffold(self.path)
        config = self.path / ".localcode/config.toml"
        config.write_text("[project]\nname = 'old'\n")
        repo.commit_paths(self.path, "Add old config", ".localcode")

        scaffold(self.path)

        self.assertFalse(config.exists())
        self.assertNotIn(
            ".localcode/config.toml",
            repo.git("ls-tree", "-r", "--name-only", "localcode", cwd=self.path),
        )

    def test_scaffold_does_not_commit_unrelated_edits(self) -> None:
        (self.path / "work.txt").write_text("unfinished\n")
        scaffold(self.path)

        status = repo.git("status", "--porcelain", cwd=self.path)
        self.assertIn("?? work.txt", status)
        self.assertNotIn(
            "work.txt",
            repo.git("ls-tree", "-r", "--name-only", "localcode", cwd=self.path),
        )

    def test_rebase_keeps_exact_localcode_snapshot(self) -> None:
        scaffold(self.path)
        wanted = self.path / ".localcode/README.md"
        wanted.write_text(wanted.read_text() + "\nAuthoritative metadata.\n")
        repo.commit_paths(self.path, "Edit localcode metadata", ".localcode")

        repo.git("switch", "main", cwd=self.path)
        accidental = self.path / ".localcode"
        # The ignored state directory survives branch switches by design.
        accidental.mkdir(exist_ok=True)
        (accidental / "config.toml").write_text("wrong\n")
        (accidental / "main-only.txt").write_text("must disappear\n")
        repo.git("add", ".localcode", cwd=self.path)
        repo.git("commit", "-m", "Accidentally add metadata", cwd=self.path)

        repo.prepare_localcode_branch(self.path)

        self.assertIn("Authoritative metadata.", wanted.read_text())
        self.assertFalse((accidental / "main-only.txt").exists())
        changed = repo.git("diff", "main...localcode", "--name-only", cwd=self.path)
        self.assertTrue(changed)
        self.assertTrue(all(name.startswith(".localcode/") for name in changed.splitlines()))

    def test_sync_absorbs_mirror_metadata_and_fast_forwards_main(self) -> None:
        scaffold(self.path)
        remote = self.root / "mirror.git"
        repo.git("init", "--bare", str(remote), cwd=self.root)
        repo.set_remote(self.path, "mirror", str(remote))
        repo.push(self.path, "mirror", "main", "localcode")

        worker = self.root / "agent"
        repo.clone(str(remote), worker)
        repo.git("config", "user.name", "Agent", cwd=worker)
        repo.git("config", "user.email", "agent@example.com", cwd=worker)

        repo.git("switch", "localcode", cwd=worker)
        (worker / ".localcode/agent.md").write_text("agent metadata\n")
        repo.git("add", ".localcode", cwd=worker)
        repo.git("commit", "-m", "Agent metadata", cwd=worker)
        repo.git("push", "origin", "localcode", cwd=worker)

        repo.git("switch", "main", cwd=worker)
        (worker / "feature.txt").write_text("merged feature\n")
        repo.git("add", "feature.txt", cwd=worker)
        repo.git("commit", "-m", "Merged feature", cwd=worker)
        repo.git("push", "origin", "main", cwd=worker)

        repo.update_mirror(self.path, "mirror")

        self.assertEqual(
            repo.git("rev-parse", "main", cwd=self.path),
            repo.git("rev-parse", "mirror/main", cwd=self.path),
        )
        self.assertEqual(
            repo.git("rev-parse", "localcode", cwd=self.path),
            repo.git(
                "ls-remote", remote.as_posix(), "refs/heads/localcode", cwd=self.path
            ).split()[0],
        )
        self.assertTrue((self.path / ".localcode/agent.md").is_file())
        self.assertTrue((self.path / "feature.txt").is_file())
        changed = repo.git("diff", "main...localcode", "--name-only", cwd=self.path)
        self.assertTrue(all(name.startswith(".localcode/") for name in changed.splitlines()))

    def test_file_at_ref_reads_main_instead_of_the_checked_out_branch(self) -> None:
        (self.path / "README.md").write_text("# Main readme\n")
        repo.git("add", "README.md", cwd=self.path)
        repo.git("commit", "-m", "Add readme", cwd=self.path)
        repo.prepare_localcode_branch(self.path)
        (self.path / "README.md").write_text("# Uncommitted metadata-branch edit\n")

        self.assertEqual(
            repo.file_at_ref(self.path, repo.MAIN_BRANCH, "README.md"),
            "# Main readme",
        )
        self.assertIsNone(
            repo.file_at_ref(self.path, repo.MAIN_BRANCH, "MISSING.md")
        )


class AgentEntrypointTest(TestCase):
    def test_functional_commit_excludes_localcode(self) -> None:
        entrypoint = Path("infra/docker/agent/entrypoint.sh").read_text()
        hello = Path("infra/docker/agent/runners/hello").read_text()
        self.assertIn("git add -A -- . ':!.localcode'", entrypoint)
        self.assertIn('HEAD:$LOCALCODE_METADATA_BRANCH', entrypoint)
        self.assertIn(">> hello.md", hello)
        self.assertNotIn(".localcode", hello)
        subprocess.run(
            ["sh", "-n", str(Path("infra/docker/agent/entrypoint.sh"))], check=True
        )

    def test_opencode_composes_persona_before_role(self) -> None:
        runner = Path("infra/docker/agent/runners/opencode").read_text()
        entrypoint = Path("infra/docker/agent/entrypoint.sh").read_text()

        self.assertLess(
            runner.index("$LOCALCODE_PERSONA_PROMPT"),
            runner.index("$LOCALCODE_ROLE_PROMPT"),
        )
        self.assertIn("# Persona: $LOCALCODE_PERSONA", runner)
        self.assertIn("# Role: $LOCALCODE_ROLE", runner)
        self.assertIn(
            "$LOCALCODE_USER/$LOCALCODE_ROLE/$LOCALCODE_RUNNER", entrypoint
        )

    def test_the_image_carries_the_scripts_the_host_points_at(self) -> None:
        """`llm/console.py` names a runtime and a path inside this image. A
        half-finished move of either is a `docker run` that fails at the far
        end, where the message is a container's, so it is checked here."""
        dockerfile = Path("infra/docker/agent/Dockerfile").read_text()
        self.assertIn("infra/docker/agent/ts/scripts/", dockerfile)
        self.assertIn("infra/docker/agent/ts/src/", dockerfile)
        self.assertIn(console.SCRIPTS, dockerfile)
        self.assertIn(f"/usr/local/bin/{console.SCRIPT_RUNTIME}", dockerfile)
        source = Path("infra/docker/agent/ts/scripts/ask.ts")
        self.assertTrue(source.is_file())
        # Both halves of that source tree, so a move that took one and left the
        # other fails here rather than inside a container.
        self.assertTrue(Path("infra/docker/agent/ts/src/llm/model.ts").is_file())
        self.assertTrue(Path("infra/docker/agent/ts/src/session/client.ts").is_file())
        self.assertTrue(Path("infra/docker/agent/ts/src/process/process.ts").is_file())
        self.assertTrue(Path("infra/docker/agent/ts/src/agent/agent.ts").is_file())
        self.assertTrue(Path("infra/docker/agent/ts/processes/chat.ts").is_file())
        self.assertTrue(Path("infra/docker/agent/ts/processes/story.ts").is_file())
        self.assertIn("infra/docker/agent/ts/processes/", dockerfile)
        subprocess.run(
            ["sh", "-n", str(Path("infra/docker/agent/process-entrypoint.sh"))],
            check=True,
        )
        self.assertTrue(console.ASK_SCRIPT.endswith(f"/{source.name}"))
        # The python this replaced, and the venv it needed.
        self.assertNotIn("/opt/ai", dockerfile)
        self.assertNotIn("ask.py", dockerfile)


class ProjectUrlTest(TestCase):
    def test_identity_and_gitea_coordinates_are_derived(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "My Project"
            localcode = path / ".localcode"
            localcode.mkdir(parents=True)
            project = Project(path, Runtime(http_port=8080))
            self.assertEqual(project.name, "My Project")
            self.assertEqual(project.gitea_owner, "localcode")
            self.assertEqual(project.gitea_repo, "my-project")
            self.assertEqual(
                project.gitea_repo_url,
                "http://localhost:8080/gitea/localcode/my-project",
            )

    def test_find_uses_committed_metadata_not_runtime_state_as_marker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "demo"
            state = path / ".localcode/state"
            state.mkdir(parents=True)
            Runtime(http_port=9090).save(state / "runtime.json")

            with self.assertRaises(NotAProject):
                Project.find(path)

            (path / ".localcode/.gitignore").write_text("/state/\n")
            project = Project.find(path)
            self.assertEqual(project.path, path.resolve())
            self.assertEqual(project.runtime.http_port, 9090)

    def test_runtime_migrates_old_admin_token_without_reusing_password(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.json"
            path.write_text(
                json.dumps(
                    {
                        "secret": "secret",
                        "admin_password": "localcode-password",
                        "admin_token": "localcode-token",
                    }
                )
            )

            runtime = Runtime.load(path)

            self.assertEqual(runtime.automation_token, "localcode-token")
            self.assertEqual(runtime.human_password, "")


class PersonaRoleDefinitionTest(TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "demo"
        localcode = self.path / ".localcode"
        (localcode / "personas").mkdir(parents=True)
        (localcode / "roles").mkdir()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_plain_markdown_catalogs_are_independent_and_ordered(self) -> None:
        personas = self.path / ".localcode/personas"
        roles = self.path / ".localcode/roles"
        (personas / "bob.md").write_text("Review for security.\n")
        (personas / "alice.md").write_text(
            "First line\nSecond line\nThird line\nFourth line\n"
        )
        (roles / "story-reviewer.md").write_text("Review the story.\n")
        # Role names are task labels, not Gitea identities, so reserved persona
        # names do not restrict them.
        (roles / "human.md").write_text("Represent the human task.\n")

        project = Project(self.path, Runtime(http_port=8080))
        definitions = project.personas()

        self.assertEqual([persona.name for persona in definitions], ["alice", "bob"])
        self.assertEqual([role.name for role in project.roles()], ["human", "story-reviewer"])
        self.assertEqual(definitions[0].preview(), "First line\nSecond line\nThird line")
        self.assertEqual(
            project.persona_file_url(definitions[0]),
            "http://localhost:8080/gitea/localcode/demo/src/branch/localcode/"
            ".localcode/personas/alice.md",
        )
        self.assertEqual(
            project.role_file_url(project.roles()[1]),
            "http://localhost:8080/gitea/localcode/demo/src/branch/localcode/"
            ".localcode/roles/story-reviewer.md",
        )
        self.assertEqual(
            project.persona_edit_url(definitions[0]),
            "http://localhost:8080/gitea/localcode/demo/_edit/localcode/"
            ".localcode/personas/alice.md",
        )
        self.assertEqual(
            project.role_edit_url(project.roles()[1]),
            "http://localhost:8080/gitea/localcode/demo/_edit/localcode/"
            ".localcode/roles/story-reviewer.md",
        )

    def test_empty_definitions_are_reported_without_hiding_valid_ones(self) -> None:
        empty_persona = self.path / ".localcode/personas/empty.md"
        empty_persona.write_text("  \n")
        valid_persona = self.path / ".localcode/personas/developer.md"
        valid_persona.write_text("Build it.\n")
        empty_role = self.path / ".localcode/roles/empty.md"
        empty_role.write_text("")
        valid_role = self.path / ".localcode/roles/custom-task.md"
        valid_role.write_text("Perform this arbitrary task.\n")

        project = Project(self.path, Runtime(http_port=8080))

        self.assertEqual([item.name for item in project.personas()], ["developer"])
        self.assertEqual([item.name for item in project.roles()], ["custom-task"])
        persona_issues = project.persona_issues()
        role_issues = project.role_issues()
        self.assertEqual(persona_issues[0].message, "instructions must not be empty")
        self.assertEqual(role_issues[0].message, "instructions must not be empty")
        self.assertEqual(
            project.persona_path_url(persona_issues[0].path),
            "http://localhost:8080/gitea/localcode/demo/src/branch/localcode/"
            ".localcode/personas/empty.md",
        )
        self.assertEqual(
            project.role_path_url(role_issues[0].path),
            "http://localhost:8080/gitea/localcode/demo/src/branch/localcode/"
            ".localcode/roles/empty.md",
        )
        self.assertEqual(
            project.persona_path_edit_url(persona_issues[0].path),
            "http://localhost:8080/gitea/localcode/demo/_edit/localcode/"
            ".localcode/personas/empty.md",
        )
        self.assertEqual(
            project.role_path_edit_url(role_issues[0].path),
            "http://localhost:8080/gitea/localcode/demo/_edit/localcode/"
            ".localcode/roles/empty.md",
        )

    def test_unreadable_text_is_reported_per_catalog(self) -> None:
        (self.path / ".localcode/personas/broken.md").write_bytes(b"\xff")
        (self.path / ".localcode/roles/broken.md").write_bytes(b"\xff")

        project = Project(self.path)

        self.assertEqual(project.personas(), [])
        self.assertEqual(project.roles(), [])
        self.assertEqual([issue.name for issue in project.persona_issues()], ["broken"])
        self.assertEqual([issue.name for issue in project.role_issues()], ["broken"])

    def test_primary_gitea_users_are_reserved_as_persona_names(self) -> None:
        personas = self.path / ".localcode/personas"
        for name in ("localcode", "human"):
            (personas / f"{name}.md").write_text("Do automated work.\n")

        project = Project(self.path)

        self.assertEqual(project.personas(), [])
        self.assertEqual(
            [issue.name for issue in project.persona_issues()], ["human", "localcode"]
        )
        self.assertTrue(
            all(
                "reserved for a primary Gitea account" in issue.message
                for issue in project.persona_issues()
            )
        )


class StoryDefinitionTest(TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "demo"
        localcode = self.path / ".localcode"
        (localcode / "stories/backlog").mkdir(parents=True)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_stories_are_parsed_and_ordered_by_number(self) -> None:
        backlog = self.path / ".localcode/stories/backlog"
        (backlog / "10-later-story.md").write_text("No frontmatter.\n")
        (backlog / "02-first-story.md").write_text(
            "---\ntitle: First story\ndate: 2026-08-28\npr_id: 17\n---\n\nBody.\n"
        )
        (backlog / "README.md").write_text("Not a story.\n")

        project = Project(self.path, Runtime(http_port=8080))
        stories = project.stories("backlog")

        self.assertEqual([story.number for story in stories], [2, 10])
        self.assertEqual(stories[0].title, "First story")
        self.assertEqual(stories[0].date, "2026-08-28")
        self.assertEqual(stories[0].pr_id, "17")
        self.assertEqual(stories[1].title, "Later story")
        self.assertEqual(
            project.story_file_url(stories[0]),
            "http://localhost:8080/gitea/localcode/demo/src/branch/localcode/"
            ".localcode/stories/backlog/02-first-story.md",
        )

    def test_unknown_story_state_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown story stage"):
            Project(self.path).stories("review")

    def test_story_process_paths_are_repo_relative_existing_story_files(self) -> None:
        story = self.path / ".localcode/stories/backlog/01-demo.md"
        story.write_text("Demo.\n")
        project = Project(self.path)

        self.assertEqual(
            project.story_path(".localcode/stories/backlog/01-demo.md"),
            ".localcode/stories/backlog/01-demo.md",
        )
        for invalid in (
            "",
            "/.localcode/stories/backlog/01-demo.md",
            ".localcode/stories/backlog/../ready/01-demo.md",
            "README.md",
            ".localcode/stories/backlog/missing.md",
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                project.story_path(invalid)


class GiteaHeaderTest(TestCase):
    def test_custom_header_keeps_only_requested_global_navigation(self) -> None:
        custom = Path("infra/docker/hub/gitea-custom/templates/custom")
        body = (custom / "body_outer_pre.tmpl").read_text()
        header = (custom / "header.tmpl").read_text()

        self.assertIn('href="/">Localcode</a>', body)
        self.assertNotIn("&larr;", body)
        self.assertIn('a.item[href$="/issues"]', header)
        self.assertIn('a.item[href$="/pulls"]', header)
        self.assertIn('a.item[href$="/milestones"]', header)
        self.assertNotIn('a.item[href$="/explore/repos"]', header)

    def test_browser_requests_are_authenticated_as_the_human_user(self) -> None:
        for name in ("Caddyfile", "Caddyfile.dev"):
            config = Path("infra/docker/hub", name).read_text()
            self.assertIn("header_up X-WEBAUTH-USER human", config)
            self.assertNotIn("header_up X-WEBAUTH-USER localcode", config)

    def test_git_clients_are_challenged_before_browser_auth_is_injected(self) -> None:
        for name in ("Caddyfile", "Caddyfile.dev"):
            config = Path("infra/docker/hub", name).read_text()
            self.assertIn("@git header User-Agent git/*", config)
            self.assertIn("@git_lfs header User-Agent git-lfs/*", config)
            self.assertIn("reverse_proxy @git 127.0.0.1:3000", config)
            self.assertIn("reverse_proxy @git_lfs 127.0.0.1:3000", config)
            self.assertLess(
                config.index("reverse_proxy @git 127.0.0.1:3000"),
                config.index("header_up X-WEBAUTH-USER human"),
            )
