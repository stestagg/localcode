"""Logging in by running opencode's own credential flow, in the agents' image.

opencode already knows how to authenticate to every provider it supports --
API keys, OAuth, device codes, whatever a given provider wants -- and that
knowledge changes with every release. Reimplementing it here would be a second,
worse copy that goes stale. So the flow is the real one: `opencode providers
login`, run in the same image the agents run, with your terminal attached.

The project's two files are mounted at the paths opencode reads, so it writes
the credential straight into `.localcode/state/` itself. This is the one thing
started from the agent image that does see part of your checkout, and it is the
point of it: an agent gets copies of these files, this writes the originals.
"""

from __future__ import annotations

from ..docker.agent import AgentContainer, ensure_image
from ..project import Project
from . import auth
from .container import AUTH_PATH, CONFIG_PATH


async def login(project: Project, provider: str | None = None) -> int:
    """Run opencode's login flow against this project's credential store."""
    await ensure_image()

    volumes = [(str(auth.ensure(project)), AUTH_PATH)]
    # Read-only: `providers login` writes credentials, never the config, and a
    # container able to write this one could commit something to the project.
    if (config := project.localcode_dir / "opencode.json").is_file():
        volumes.append((str(config), f"{CONFIG_PATH}:ro"))

    return await AgentContainer(
        project=project,
        name=f"localcode-auth-{project.id}",
        role="auth",
        volumes=volumes,
        entrypoint="opencode",
        command=["providers", "login", *(["-p", provider] if provider else [])],
    ).attach()
