"""Getting a project's LLM configuration into a container.

opencode reads two files: its config at `$XDG_CONFIG_HOME/opencode/opencode.json`
and its credentials at `$XDG_DATA_HOME/opencode/auth.json`. A container mounts
nothing from the host -- that is the whole point of it, it cannot reach your
checkout -- so both arrive as environment variables, and the agent entrypoint
writes them to those paths before the runner starts. The `ask` container reads
the same two variables directly, having no opencode to satisfy.

That does put credentials where `docker inspect` can see them, the same as the
gitea token already is. The alternative, bind-mounting the host's auth file,
would either be read-only (breaking opencode's token refresh) or would let a
container write back into `.localcode/state/`. A container that can be inspected
by whoever started it is the better trade.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from ..project import Project
from . import auth
from .config import PLACEHOLDER, ConfigError, env_placeholders, find

#: The env vars the entrypoint turns back into files.
CONFIG_ENV = "LOCALCODE_OPENCODE_CONFIG"
AUTH_ENV = "LOCALCODE_OPENCODE_AUTH"

#: Where the entrypoint writes them. Kept here so the two ends agree in one
#: place, even though only the entrypoint uses the paths.
HOME = "/root"
CONFIG_PATH = f"{HOME}/.config/opencode/opencode.json"
AUTH_PATH = f"{HOME}/.local/share/opencode/auth.json"

#: How a container reaches a server running on your machine. The agent gets
#: `--add-host host.docker.internal:host-gateway` for exactly this.
HOST_GATEWAY = "host.docker.internal"

#: The authority half of a url, when it names this machine. Only ever matched
#: straight after `//`, so it cannot touch anything that is not a host.
LOOPBACK = re.compile(r"(?<=//)(localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\])(?=[:/]|$)")


def _inline_files(text: str, *, base: Path) -> str:
    """Expand `{file:...}` placeholders, which name paths only the host has.

    `{env:...}` is deliberately left alone: the container expands those itself
    against the variables `environment()` forwards, which keeps a config that
    reads a key from the environment doing exactly that at both ends.
    """

    def expand(match: re.Match[str]) -> str:
        if match.group(1) != "file":
            return match.group(0)
        target = match.group(2).strip()
        path = Path(target).expanduser()
        path = path if path.is_absolute() else base / path
        try:
            value = path.read_text().strip()
        except OSError as exc:
            raise ConfigError(f"{{file:{target}}}: {exc}") from exc
        # Straight into a JSON string literal, so it has to be escaped as one.
        return json.dumps(value)[1:-1]

    return PLACEHOLDER.sub(expand, text)


def _reach_the_host(text: str) -> str:
    """Point loopback urls at the machine rather than at the container.

    `http://localhost:11434` means one thing where you typed it and something
    else entirely inside a container, where it is the container itself. A local
    model server is the whole reason this matters, and the config cannot say
    both at once -- so the host keeps the url it wrote and the container gets
    the one that reaches back out.
    """
    return LOOPBACK.sub(HOST_GATEWAY, text)


def environment(project: Project) -> dict[str, str]:
    """The LLM half of an agent container's environment.

    Empty when the project has no config and no credentials, so an agent that
    runs a shell-script runner is not made to care about any of this.

    The config is passed on as written apart from two host-only facts: `{file:}`
    placeholders name paths only this machine has, and a loopback url means the
    wrong thing on the other side. `{env:}` is left for the container to expand.
    """
    env: dict[str, str] = {}

    path = find(project)
    if path is not None:
        text = path.read_text()
        env[CONFIG_ENV] = _reach_the_host(_inline_files(text, base=path.parent))
        for name in sorted(env_placeholders(text)):
            if (value := os.environ.get(name)) is not None:
                env[name] = value

    if credentials := auth.text(project):
        env[AUTH_ENV] = credentials

    return env
