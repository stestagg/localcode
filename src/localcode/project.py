"""A localcode project: the repo, the paths under `.localcode/`, and runtime state.

Everything localcode knows about a project is derived from the repo it lives in.
Anything a run generates -- ports, the runtime secret, gitea credentials -- is
`.localcode/state/runtime.json`, which is gitignored and mode 0600.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath
from urllib.parse import quote

RUNTIME_NAME = "runtime.json"
OPENCODE_AUTH_NAME = "opencode-auth.json"
PROCESS_LOGS_DIR = "process_logs"
SESSIONS_DIR = "sessions"
LOCALCODE_DIR = ".localcode"
PROJECT_MARKER = ".gitignore"
GITEA_OWNER = "localcode"
GITEA_HUMAN = "human"
GITEA_RESERVED_USERS = frozenset({GITEA_OWNER, GITEA_HUMAN})
DEFAULT_HTTP_PORT = 8080
STORY_STAGES = ("backlog", "ready", "in_progress", "done", "cancelled")
STORY_NAME = re.compile(r"^(\d+)-.+\.md$")
#: Anything that has no business in a filename localcode generates.
UNSAFE_IN_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def _safe(value: str) -> str:
    """`value` with anything that cannot go in a filename replaced."""
    return UNSAFE_IN_NAME.sub("-", value) or "unnamed"


def project_slug(name: str) -> str:
    """A repository name Gitea will accept, derived from a directory name."""
    kept = [
        character if character.isalnum() or character in "-_." else "-"
        for character in name.lower()
    ]
    return "".join(kept).strip("-.") or "project"


class NotAProject(Exception):
    """Raised when a path has no `.localcode/` at or above it."""


class DefinitionError(ValueError):
    """Raised when a persona or role markdown file is invalid."""


@dataclass(frozen=True)
class Persona:
    """One reusable perspective, loaded from `.localcode/personas/<name>.md`."""

    name: str
    prompt: str
    path: Path

    def preview(self, lines: int = 3) -> str:
        """The first few prompt lines for compact displays."""
        return "\n".join(self.prompt.splitlines()[:lines]).strip()


@dataclass(frozen=True)
class Role:
    """One reusable task description, loaded from `.localcode/roles/<name>.md`."""

    name: str
    prompt: str
    path: Path

    def preview(self, lines: int = 3) -> str:
        """The first few prompt lines for compact displays."""
        return "\n".join(self.prompt.splitlines()[:lines]).strip()


@dataclass(frozen=True)
class DefinitionIssue:
    """A malformed persona or role file that was skipped while loading."""

    name: str
    message: str
    path: Path


@dataclass(frozen=True)
class Story:
    """A story in one lifecycle directory under `.localcode/stories/`."""

    number: int
    title: str
    date: str
    pr_id: str
    stage: str
    path: Path


def _story_from_path(path: Path, stage: str) -> Story | None:
    """Read the small frontmatter subset displayed in the stories view."""
    match = STORY_NAME.match(path.name)
    if match is None:
        return None

    metadata: dict[str, str] = {}
    try:
        source = path.read_text().splitlines()
    except (OSError, UnicodeError):
        source = []
    if source and source[0].strip() == "---":
        for line in source[1:]:
            if line.strip() == "---":
                break
            key, separator, value = line.partition(":")
            if separator:
                metadata[key.strip()] = value.strip().strip("'\"")

    fallback = path.stem.split("-", 1)[1].replace("-", " ").capitalize()
    return Story(
        number=int(match.group(1)),
        title=metadata.get("title") or fallback,
        date=metadata.get("date", ""),
        pr_id=metadata.get("pr_id", ""),
        stage=stage,
        path=path,
    )


def _definition_prompt(path: Path) -> str:
    """Read a plain-Markdown definition, rejecting instructions with no text."""
    prompt = path.read_text().strip()
    if not prompt:
        raise DefinitionError(f"{path}: instructions must not be empty")
    return prompt


def _persona_from_path(path: Path) -> Persona:
    """Load a persona whose name is also safe to use as a Gitea identity."""
    if path.stem in GITEA_RESERVED_USERS:
        raise DefinitionError(
            f"{path}: {path.stem!r} is reserved for a primary Gitea account"
        )
    return Persona(name=path.stem, prompt=_definition_prompt(path), path=path)


def _role_from_path(path: Path) -> Role:
    """Load an unrestricted task role from plain Markdown."""
    return Role(name=path.stem, prompt=_definition_prompt(path), path=path)


@dataclass
class Runtime:
    """What one `localcode run` generated, as stored in state/runtime.json."""

    secret: str = ""
    http_port: int = 0
    ws_port: int = 0
    pid: int = 0
    human_password: str = ""
    automation_token: str = ""
    #: persona name -> that persona's gitea access token.
    personas: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> Runtime:
        if not path.exists():
            return cls()
        state = json.loads(path.read_text())
        # Before the browser and automation identities were split, `localcode`
        # was the admin and both of its credentials used these names. Its token
        # is still the automation token. Its password must not be presented as
        # the new `human` user's password, so provisioning will generate one.
        legacy_token = state.pop("admin_token", "")
        state.pop("admin_password", None)
        if not state.get("automation_token"):
            state["automation_token"] = legacy_token
        return cls(**state)

    def save(self, path: Path) -> None:
        # Written 0600 before any content lands in it: the file carries the
        # runtime secret, gitea credentials, and every persona token.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(asdict(self), handle, indent=2)
            handle.write("\n")


@dataclass(frozen=True)
class Project:
    """A repo set up for localcode, and every path that follows from it."""

    path: Path
    runtime: Runtime = field(default_factory=Runtime)

    @classmethod
    def find(cls, start: Path | str | None = None) -> Project:
        """The nearest project at or above `start`.

        Walking up means `localcode run` works from anywhere in the tree, the
        way git itself does.
        """
        here = Path(start or Path.cwd()).resolve()
        for candidate in (here, *here.parents):
            if (candidate / LOCALCODE_DIR / PROJECT_MARKER).is_file():
                project = cls(path=candidate)
                return cls(path=candidate, runtime=Runtime.load(project.runtime_path))
        raise NotAProject(f"no {LOCALCODE_DIR}/ at or above {here}")

    # --- identity -----------------------------------------------------------

    @property
    def id(self) -> str:
        """Stable per-checkout id, so several projects coexist on one docker."""
        return hashlib.sha256(str(self.path).encode()).hexdigest()[:8]

    @property
    def hub_container(self) -> str:
        return f"localcode-hub-{self.id}"

    @property
    def network(self) -> str:
        return f"localcode-{self.id}"

    def agent_container(self, run_id: str) -> str:
        return f"localcode-agent-{self.id}-{run_id}"

    def ask_container(self, run_id: str) -> str:
        """One question's container. Named per run: the web ui can ask twice
        at once, and two containers cannot share a name."""
        return f"localcode-ask-{self.id}-{_safe(run_id)}"

    def process_container(self, run_id: str) -> str:
        """The isolated container running one long-lived process session."""
        return f"localcode-process-{self.id}-{_safe(run_id)}"

    @property
    def labels(self) -> dict[str, str]:
        return {"localcode.project": self.id, "localcode.path": str(self.path)}

    # --- committed layout ---------------------------------------------------

    @property
    def localcode_dir(self) -> Path:
        return self.path / LOCALCODE_DIR

    @property
    def personas_dir(self) -> Path:
        return self.localcode_dir / "personas"

    @property
    def roles_dir(self) -> Path:
        return self.localcode_dir / "roles"

    @property
    def stories_dir(self) -> Path:
        return self.localcode_dir / "stories"

    def stories(self, stage: str) -> list[Story]:
        """Stories in one known lifecycle stage, ordered by story number."""
        if stage not in STORY_STAGES:
            raise ValueError(f"unknown story stage: {stage}")
        stories = [
            story
            for path in self.stories_dir.joinpath(stage).glob("*.md")
            if (story := _story_from_path(path, stage)) is not None
        ]
        return sorted(stories, key=lambda story: (story.number, story.path.name))

    def story_path(self, value: object) -> str:
        """Validate one repo-relative story path supplied to a workflow.

        A process clones the metadata branch elsewhere, so the portable input
        is the path relative to the repository, never a path on this host.
        Restricting it to the known story layout also keeps an untrusted socket
        value from becoming an arbitrary file made available to an agent.
        """
        if not isinstance(value, str) or not value.strip():
            raise ValueError("story is required")

        path = PurePosixPath(value)
        parts = path.parts
        if (
            path.is_absolute()
            or len(parts) != 4
            or parts[:2] != (LOCALCODE_DIR, "stories")
            or parts[2] not in STORY_STAGES
            or STORY_NAME.fullmatch(parts[3]) is None
        ):
            raise ValueError(
                "story must be a repo-relative path under "
                ".localcode/stories/<stage>/"
            )

        relative = path.as_posix()
        if not (self.path / relative).is_file():
            raise ValueError(f"story does not exist: {relative}")
        return relative

    def story_file_url(self, story: Story) -> str:
        """The story's source page on the metadata branch in Gitea."""
        filename = quote(story.path.name, safe="")
        return (
            f"{self.gitea_repo_url}/src/branch/localcode/"
            f".localcode/stories/{story.stage}/{filename}"
        )

    def personas(self) -> list[Persona]:
        """All valid personas, leaving malformed files isolated."""
        personas, _ = self.load_personas()
        return personas

    def persona_issues(self) -> list[DefinitionIssue]:
        """Validation problems for persona files that could not be loaded."""
        _, issues = self.load_personas()
        return issues

    def load_personas(self) -> tuple[list[Persona], list[DefinitionIssue]]:
        """Load valid personas and report bad files without failing the project."""
        personas: list[Persona] = []
        issues: list[DefinitionIssue] = []
        for path in sorted(self.personas_dir.glob("*.md")):
            try:
                personas.append(_persona_from_path(path))
            except (DefinitionError, OSError, UnicodeError) as exc:
                message = str(exc).removeprefix(f"{path}: ")
                issues.append(DefinitionIssue(path.stem, message, path))
        return personas, issues

    def roles(self) -> list[Role]:
        """All valid task roles, leaving malformed files isolated."""
        roles, _ = self.load_roles()
        return roles

    def role_issues(self) -> list[DefinitionIssue]:
        """Validation problems for role files that could not be loaded."""
        _, issues = self.load_roles()
        return issues

    def load_roles(self) -> tuple[list[Role], list[DefinitionIssue]]:
        """Load valid roles and report bad files without failing the project."""
        roles: list[Role] = []
        issues: list[DefinitionIssue] = []
        for path in sorted(self.roles_dir.glob("*.md")):
            try:
                roles.append(_role_from_path(path))
            except (DefinitionError, OSError, UnicodeError) as exc:
                message = str(exc).removeprefix(f"{path}: ")
                issues.append(DefinitionIssue(path.stem, message, path))
        return roles, issues

    def persona(self, name: str) -> Persona | None:
        """Find a named persona without allowing a filename/path traversal."""
        return next((persona for persona in self.personas() if persona.name == name), None)

    def role(self, name: str) -> Role | None:
        """Find a named role without allowing a filename/path traversal."""
        return next((role for role in self.roles() if role.name == name), None)

    def persona_file_url(self, persona: Persona) -> str:
        """The browser page for a persona definition on the metadata branch."""
        return self.persona_path_url(persona.path)

    def persona_edit_url(self, persona: Persona) -> str:
        """The Gitea editor for a persona definition on the metadata branch."""
        return self.persona_path_edit_url(persona.path)

    def persona_path_url(self, path: Path) -> str:
        """The browser page for any persona file, including an invalid one."""
        filename = quote(path.name, safe="")
        return (
            f"{self.gitea_repo_url}/src/branch/localcode/"
            f".localcode/personas/{filename}"
        )

    def persona_path_edit_url(self, path: Path) -> str:
        """The Gitea editor for any persona file, including an invalid one."""
        filename = quote(path.name, safe="")
        return (
            f"{self.gitea_repo_url}/_edit/localcode/"
            f".localcode/personas/{filename}"
        )

    def role_file_url(self, role: Role) -> str:
        """The browser page for a role definition on the metadata branch."""
        return self.role_path_url(role.path)

    def role_edit_url(self, role: Role) -> str:
        """The Gitea editor for a role definition on the metadata branch."""
        return self.role_path_edit_url(role.path)

    def role_path_url(self, path: Path) -> str:
        """The browser page for any role file, including an invalid one."""
        filename = quote(path.name, safe="")
        return (
            f"{self.gitea_repo_url}/src/branch/localcode/"
            f".localcode/roles/{filename}"
        )

    def role_path_edit_url(self, path: Path) -> str:
        """The Gitea editor for any role file, including an invalid one."""
        filename = quote(path.name, safe="")
        return (
            f"{self.gitea_repo_url}/_edit/localcode/"
            f".localcode/roles/{filename}"
        )

    # --- state (gitignored) -------------------------------------------------

    @property
    def state_dir(self) -> Path:
        return self.localcode_dir / "state"

    @property
    def runtime_path(self) -> Path:
        return self.state_dir / RUNTIME_NAME

    @property
    def opencode_auth_path(self) -> Path:
        """Provider credentials, in opencode's own auth.json format.

        Under `state/` rather than beside `opencode.json`: the config is
        committed so a project's model choice travels with it, and the keys
        that make it work must not.
        """
        return self.state_dir / OPENCODE_AUTH_NAME

    @property
    def process_log_dir(self) -> Path:
        """Where the raw output of everything localcode spawns is kept.

        Under `state/` so it is gitignored, and so `watch.py` -- which prunes
        that directory rather than walking it -- does not re-hash a growing log
        once a second.
        """
        return self.state_dir / PROCESS_LOGS_DIR

    def process_log(self, action: str, run_id: str) -> Path:
        """The transcript for one run, named so `ls` sorts it chronologically."""
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        return self.process_log_dir / f"{stamp}-{_safe(action)}-{_safe(run_id)}.log"

    @property
    def sessions_dir(self) -> Path:
        """Where each agent session's message record is appended."""
        return self.state_dir / SESSIONS_DIR

    def session_path(self, session_id: str) -> Path:
        return self.sessions_dir / f"{_safe(session_id)}.jsonl"

    @property
    def gitea_dir(self) -> Path:
        return self.state_dir / "gitea"

    @property
    def gitea_conf(self) -> Path:
        return self.gitea_dir / "conf" / "app.ini"

    @property
    def caddy_dir(self) -> Path:
        return self.state_dir / "caddy"

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def gitea_owner(self) -> str:
        return GITEA_OWNER

    @property
    def gitea_repo(self) -> str:
        return project_slug(self.name)

    # --- urls ---------------------------------------------------------------

    @property
    def http_url(self) -> str:
        return f"http://localhost:{self.runtime.http_port}/"

    @property
    def gitea_url(self) -> str:
        return f"{self.http_url}gitea/"

    @property
    def gitea_repo_url(self) -> str:
        """The browser page for this project's repository."""
        return f"{self.gitea_url}{self.gitea_owner}/{self.gitea_repo}"

    @property
    def gitea_api(self) -> str:
        return f"{self.http_url}gitea/api/v1"

    @property
    def internal_gitea_url(self) -> str:
        """How an agent container reaches gitea: the hub's alias on the network."""
        return "http://localcode/gitea"

    def push_url(self, user: str, token: str) -> str:
        """A clone url with credentials, for pushing from the host.

        Pushes go over http rather than straight into the bare repo on disk:
        gitea's receive hooks have to run inside the container, where the gitea
        binary they call actually exists.
        """
        return (
            f"http://{user}:{token}@localhost:{self.runtime.http_port}"
            f"/gitea/{self.gitea_owner}/{self.gitea_repo}.git"
        )

    def save_runtime(self) -> None:
        self.runtime.save(self.runtime_path)
