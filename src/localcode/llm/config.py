"""Finding `.localcode/opencode.json`, and nothing more.

This used to parse opencode's config format. It no longer does, and that is the
point: the file belongs to opencode, and the two things that read it -- the
`opencode` binary in an agent container, and the AI SDK script in an `ask` one
-- both understand it far better than this ever could. So the host locates it
and hands over its bytes unchanged.

The one thing that cannot wait until the far end is `{env:NAME}`: a container
gets its own environment, so the variables a config names have to be forwarded
from out here or they expand to nothing over there.

Credentials are never in this file. It is committed; they live in
`state/opencode-auth.json`, which is not. See `auth.py`.
"""

from __future__ import annotations

import re
from pathlib import Path

from ..project import Project

#: Tried in order. `.jsonc` first, since a project that has one means it.
CONFIG_NAMES = ("opencode.jsonc", "opencode.json")

#: `{env:NAME}` and `{file:path}`, opencode's two config placeholders.
PLACEHOLDER = re.compile(r"\{(env|file):([^}]+)\}")


class ConfigError(Exception):
    """Raised when `.localcode/opencode.json` exists but cannot be used."""


def find(project: Project) -> Path | None:
    """The project's config file, if it has written one."""
    for name in CONFIG_NAMES:
        candidate = project.localcode_dir / name
        if candidate.is_file():
            return candidate
    return None


def env_placeholders(text: str) -> set[str]:
    """Every `{env:NAME}` a config's raw text refers to."""
    return {
        match.group(2).strip()
        for match in PLACEHOLDER.finditer(text)
        if match.group(1) == "env"
    }
