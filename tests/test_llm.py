import json
import os
import tempfile
from pathlib import Path
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import patch

import httpx
from click.testing import CliRunner

from localcode.cli import _pick_model
from localcode.docker.agent import HOST_GATEWAY, IMAGE, AgentContainer
from localcode.llm import auth, console, container, llamacpp
from localcode.llm.config import env_placeholders, find
from localcode.project import Project

# Provider ids throughout are made up. Nothing in localcode knows one provider
# from another, and a test that used a real name would suggest otherwise.
MODEL = "acme/model-one"


class ProjectCase(TestCase):
    """A project on disk, with nothing in it but the directories."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.project = Project(path=Path(self.temp.name))
        self.project.state_dir.mkdir(parents=True)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write_config(self, data: dict | str, name: str = "opencode.json") -> Path:
        path = self.project.localcode_dir / name
        path.write_text(data if isinstance(data, str) else json.dumps(data))
        return path

    def write_auth(self, credentials: dict) -> None:
        """The store, as opencode leaves it. Nothing in localcode writes one."""
        self.project.opencode_auth_path.write_text(
            json.dumps({p: {"type": "api", "key": k} for p, k in credentials.items()})
        )


class ConfigTest(ProjectCase):
    """The host locates the file and notices what it references. That is all it
    does with opencode's format -- parsing happens where the file is read."""

    def test_absent_config_is_not_an_error(self) -> None:
        self.assertIsNone(find(self.project))

    def test_jsonc_is_preferred_over_json(self) -> None:
        self.write_config({"model": "acme/a"}, name="opencode.json")
        self.write_config({"model": "acme/b"}, name="opencode.jsonc")
        self.assertEqual(find(self.project).name, "opencode.jsonc")

    def test_either_name_is_found(self) -> None:
        self.write_config({"model": MODEL}, name="opencode.json")
        self.assertEqual(find(self.project).name, "opencode.json")

    def test_a_config_that_is_not_json_is_still_found(self) -> None:
        """Nothing here parses it, so nothing here can reject it. Whatever
        reads it at the far end is what gets to complain."""
        self.write_config("{not json")
        self.assertIsNotNone(find(self.project))

    def test_env_placeholders_are_found_in_raw_text(self) -> None:
        text = '{"a": "{env:ONE}", "b": "{file:x}", "c": "{env:TWO}"}'
        self.assertEqual(env_placeholders(text), {"ONE", "TWO"})


class AuthTest(ProjectCase):
    """Everything localcode does with the credential store: notice whether it
    has one, and make an empty one to mount. It never reads or writes inside."""

    def test_no_store_is_no_text_rather_than_an_error(self) -> None:
        self.assertEqual(auth.text(self.project), "")

    def test_the_stores_bytes_come_back_unchanged(self) -> None:
        raw = '{"acme": {"type": "oauth", "refresh": "r", "access": "a"}}'
        self.project.opencode_auth_path.write_text(raw)
        self.assertEqual(auth.text(self.project), raw)

    def test_a_store_it_cannot_understand_is_carried_anyway(self) -> None:
        """opencode writes shapes localcode has no opinion about, and newer
        opencodes will write ones it has never seen. Nothing parses it, so
        there is nothing here for any of them to be rejected by."""
        raw = '{"corp": {"type": "something-invented-later", "blob": [1, 2]}}'
        self.project.opencode_auth_path.write_text(raw)
        self.assertEqual(auth.text(self.project), raw)

    def test_ensure_creates_an_empty_store_0600(self) -> None:
        path = auth.ensure(self.project)
        self.assertTrue(path.is_file())
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(path.read_text(), auth.EMPTY)

    def test_ensure_leaves_an_existing_store_alone(self) -> None:
        self.write_auth({"acme": "sk-test"})
        before = self.project.opencode_auth_path.read_text()
        self.assertEqual(auth.ensure(self.project).read_text(), before)


class ContainerTest(ProjectCase):
    def test_nothing_is_passed_when_nothing_is_configured(self) -> None:
        self.assertEqual(container.environment(self.project), {})

    def test_config_travels_verbatim(self) -> None:
        text = '{\n  // keep me\n  "model": "acme/model-one",\n}\n'
        self.write_config(text)
        env = container.environment(self.project)
        self.assertEqual(env[container.CONFIG_ENV], text)

    def test_env_placeholders_are_forwarded_not_expanded(self) -> None:
        self.write_config({"provider": {"a": {"options": {"apiKey": "{env:SOME_KEY}"}}}})
        os.environ["SOME_KEY"] = "value"
        try:
            env = container.environment(self.project)
        finally:
            del os.environ["SOME_KEY"]
        self.assertIn("{env:SOME_KEY}", env[container.CONFIG_ENV])
        self.assertEqual(env["SOME_KEY"], "value")

    def test_file_placeholders_are_inlined_since_only_the_host_has_them(self) -> None:
        (self.project.localcode_dir / "key.txt").write_text('a"b\n')
        self.write_config({"provider": {"a": {"options": {"apiKey": "{file:key.txt}"}}}})
        env = container.environment(self.project)
        self.assertNotIn("{file:", env[container.CONFIG_ENV])
        loaded = json.loads(env[container.CONFIG_ENV])
        self.assertEqual(loaded["provider"]["a"]["options"]["apiKey"], 'a"b')

    def test_loopback_urls_are_pointed_back_at_the_host(self) -> None:
        self.write_config(
            {
                "model": "local/m",
                "provider": {
                    "local": {"options": {"baseURL": "http://localhost:11434/v1"}}
                },
            }
        )
        shipped = json.loads(container.environment(self.project)[container.CONFIG_ENV])
        self.assertEqual(
            shipped["provider"]["local"]["options"]["baseURL"],
            "http://host.docker.internal:11434/v1",
        )

    def test_a_remote_url_is_left_alone(self) -> None:
        self.write_config({"model": MODEL, "note": "localhost in prose"})
        shipped = container.environment(self.project)[container.CONFIG_ENV]
        self.assertIn("localhost in prose", shipped)
        self.assertNotIn("host.docker.internal", shipped)

    def test_credentials_are_passed_as_the_auth_file(self) -> None:
        self.write_auth({"acme": "sk-test"})
        env = container.environment(self.project)
        self.assertEqual(
            json.loads(env[container.AUTH_ENV]),
            {"acme": {"type": "api", "key": "sk-test"}},
        )


class AgentContainerTest(ProjectCase):
    """The one place a `docker run` gets built. Not that it runs -- that it is
    right, since every container localcode starts comes out of here."""

    def container(self, **kwargs) -> AgentContainer:
        return AgentContainer(
            project=self.project,
            name=self.project.ask_container("test"),
            role="ask",
            env={
                **container.environment(self.project),
                console.SYSTEM_ENV: "",
            },
            entrypoint=console.SCRIPT_RUNTIME,
            command=[console.ASK_SCRIPT],
            **kwargs,
        )

    def test_every_container_is_thrown_away(self) -> None:
        self.assertIn("--rm", self.container().argv())

    def test_the_entrypoint_and_command_are_the_last_words(self) -> None:
        argv = self.container().argv()
        self.assertEqual(argv[-1], console.ASK_SCRIPT)
        self.assertEqual(argv[argv.index("--entrypoint") + 1], console.SCRIPT_RUNTIME)
        self.assertEqual(argv[argv.index("--entrypoint") + 2], IMAGE)

    def test_stdin_and_tty_are_off_unless_asked_for(self) -> None:
        self.assertNotIn("--interactive", self.container().argv())
        self.assertNotIn("--tty", self.container().argv())
        self.assertIn("--interactive", self.container().argv(stdin=True))
        self.assertIn("--tty", self.container().argv(stdin=True, tty=True))

    def test_a_model_on_this_machine_can_be_reached(self) -> None:
        argv = self.container().argv()
        self.assertIn("--add-host", argv)
        self.assertEqual(argv[argv.index("--add-host") + 1], HOST_GATEWAY)

    def test_the_role_labels_it_and_extra_labels_survive(self) -> None:
        argv = self.container(labels={"localcode.runner": "hello"}).argv()
        labels = {argv[i + 1] for i, a in enumerate(argv) if a == "--label"}
        self.assertIn("localcode.role=ask", labels)
        self.assertIn("localcode.runner=hello", labels)
        self.assertIn(f"localcode.project={self.project.id}", labels)

    def test_volumes_are_mounted_where_asked(self) -> None:
        argv = self.container(volumes=[("/host/auth.json", "/in/auth.json")]).argv()
        self.assertEqual(argv[argv.index("-v") + 1], "/host/auth.json:/in/auth.json")

    def test_the_prompt_is_not_in_the_environment(self) -> None:
        """It goes in on stdin, so it stays out of `docker inspect`."""
        self.write_config({"model": MODEL})
        self.assertNotIn("prompt", " ".join(self.container().argv()).lower())

    def test_the_config_carries_the_selection_rather_than_a_second_variable(
        self,
    ) -> None:
        """The container reads which model out of the config it is given, so
        there is no separate model variable to disagree with the file."""
        self.write_config({"model": MODEL})
        self.write_auth({"acme": "sk-test"})
        argv = self.container().argv()
        passed = {argv[i + 1].split("=", 1)[0] for i, a in enumerate(argv) if a == "-e"}
        self.assertIn(container.CONFIG_ENV, passed)
        self.assertIn(container.AUTH_ENV, passed)
        self.assertIn(MODEL, " ".join(argv))
        self.assertNotIn("LOCALCODE_ASK_MODEL", passed)


class LlamaCppTest(IsolatedAsyncioTestCase):
    """What llama-server is asked, and the config made of its answers."""

    LISTED = {"data": [{"id": "a-model"}, {"id": "b-model"}]}

    async def config(self, props: dict) -> dict:
        def respond(request: httpx.Request) -> httpx.Response:
            listed = request.url.path.endswith("/models")
            return httpx.Response(200, json=self.LISTED if listed else props)

        return await llamacpp.config(
            "http://llama.test:8080/v1", transport=httpx.MockTransport(respond)
        )

    async def test_the_models_the_server_serves_are_the_ones_written(self) -> None:
        provider = (await self.config({}))["provider"]["llama.cpp"]
        self.assertEqual(list(provider["models"]), ["a-model", "b-model"])
        self.assertEqual(provider["options"]["baseURL"], "http://llama.test:8080/v1")
        # Empty on purpose: this server wants no key, and no field means the
        # opposite -- `llm ask` goes looking for one and fails without it.
        self.assertEqual(provider["options"]["apiKey"], "")

    async def test_the_window_the_server_was_started_with_is_the_limit(self) -> None:
        written = await self.config({"default_generation_settings": {"n_ctx": 32768}})
        limit = written["provider"]["llama.cpp"]["models"]["a-model"]["limit"]
        self.assertEqual(limit, {"context": 32768, "output": 16384})

    async def test_a_router_reports_no_window_so_none_is_claimed(self) -> None:
        """It serves several models and has no one context to report. opencode
        has defaults; a number made up here would be worse than none."""
        written = await self.config({"role": "router", "n_ctx": 0})
        self.assertNotIn("limit", written["provider"]["llama.cpp"]["models"]["a-model"])

    async def test_a_server_that_is_not_llama_server_says_so(self) -> None:
        def respond(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"not": "llama.cpp"})

        with self.assertRaises(llamacpp.LlamaError):
            await llamacpp.config("http://x:1", transport=httpx.MockTransport(respond))


class LlamaCppSelectTest(TestCase):
    """Naming one of the served models as the project default."""

    CONFIG = {
        "$schema": "https://opencode.ai/config.json",
        "provider": {"llama.cpp": {"models": {"a-model": {}, "vendor/b-model": {}}}},
    }

    def test_the_listed_ids_come_back_in_the_order_the_server_gave_them(self) -> None:
        self.assertEqual(llamacpp.models(self.CONFIG), ["a-model", "vendor/b-model"])

    def test_the_selection_is_qualified_with_the_provider(self) -> None:
        """opencode resolves `provider/model`; a bare id means search them all,
        and this provider is not in the catalogue to be searched."""
        selected = llamacpp.select(self.CONFIG, "a-model")
        self.assertEqual(selected["model"], "llama.cpp/a-model")

    def test_an_id_with_a_slash_in_it_keeps_it(self) -> None:
        """A server started on a file names its model after one, so half of
        them are `vendor/repo` already: `llama.cpp/vendor/repo` is the id."""
        selected = llamacpp.select(self.CONFIG, "vendor/b-model")
        self.assertEqual(selected["model"], "llama.cpp/vendor/b-model")

    def test_it_is_the_same_config_otherwise_and_reads_before_the_block(self) -> None:
        selected = llamacpp.select(self.CONFIG, "a-model")
        self.assertEqual(selected["provider"], self.CONFIG["provider"])
        self.assertEqual(list(selected), ["$schema", "model", "provider"])
        self.assertNotIn("model", self.CONFIG)


class PickModelTest(TestCase):
    """The terminal side of the choice: what is shown, and what is asked."""

    def pick(self, models: list[str], typed: str = "") -> tuple[str, str]:
        runner = CliRunner()
        with runner.isolation(input=typed) as streams:
            chosen = _pick_model(models)
        return chosen, streams[0].getvalue().decode()

    def test_every_model_is_listed_and_the_typed_number_wins(self) -> None:
        chosen, shown = self.pick(["a-model", "b-model", "c-model"], "3\n")
        self.assertEqual(chosen, "c-model")
        for model in ("a-model", "b-model", "c-model"):
            self.assertIn(model, shown)

    def test_the_first_is_the_default_for_an_empty_answer(self) -> None:
        self.assertEqual(self.pick(["a-model", "b-model"], "\n")[0], "a-model")

    def test_one_model_is_shown_but_not_asked_about(self) -> None:
        """No stdin to read: a question with one answer would only block."""
        chosen, shown = self.pick(["only-model"])
        self.assertEqual(chosen, "only-model")
        self.assertIn("only-model", shown)


class ConfigureLlamaCppTest(ProjectCase, IsolatedAsyncioTestCase):
    """The command: ask the server, ask the caller, write the answer."""

    SERVED = {
        "$schema": "https://opencode.ai/config.json",
        "provider": {"llama.cpp": {"models": {"a-model": {}, "b-model": {}}}},
    }

    async def configure(self, **kwargs) -> dict:
        with patch.object(llamacpp, "config", return_value=self.SERVED):
            path = await console.configure_llamacpp(self.project, **kwargs)
        return json.loads(path.read_text())

    async def test_the_chooser_sees_the_served_models_and_picks_the_default(
        self,
    ) -> None:
        seen: list[list[str]] = []

        def choose(models: list[str]) -> str:
            seen.append(models)
            return models[1]

        written = await self.configure(choose=choose)
        self.assertEqual(seen, [["a-model", "b-model"]])
        self.assertEqual(written["model"], "llama.cpp/b-model")

    async def test_without_a_chooser_nothing_is_selected(self) -> None:
        """Every caller that has a terminal passes one. One that does not gets
        the provider block and no opinion about which model to run."""
        self.assertNotIn("model", await self.configure())


class LlamaCppWriteTest(ProjectCase):
    def test_it_writes_the_config_it_was_given(self) -> None:
        path = llamacpp.write(self.project, {"model": MODEL})
        self.assertEqual(path, self.project.localcode_dir / "opencode.json")
        self.assertEqual(json.loads(path.read_text()), {"model": MODEL})
