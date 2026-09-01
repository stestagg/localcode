"""`.localcode/state/opencode-auth.json`: provider credentials, kept out of git.

Byte-for-byte the file opencode keeps at `~/.local/share/opencode/auth.json`, so
`localcode llm configure` and `opencode auth login` produce interchangeable
files and a container can be handed this one unchanged.

Nothing here reads a credential, and nothing here parses the file. opencode's
login flow writes it -- mounted at the path opencode expects, so it is written
in place -- and a container is handed its bytes. localcode never needs to know
what is in it, only whether there is anything.

It is under `state/`, which `.localcode/.gitignore` excludes, and it is created
0600 the same way the runtime secret is.
"""

from __future__ import annotations

import os
from pathlib import Path

from ..project import Project

#: What an empty store looks like, for a file that has to exist before it can
#: be mounted. The only thing here that knows the format at all.
EMPTY = "{}\n"


def text(project: Project) -> str:
    """The store's contents, for handing to a container. Empty when there is none."""
    try:
        return project.opencode_auth_path.read_text().strip()
    except OSError:
        return ""


def ensure(project: Project) -> Path:
    """The store, created empty if absent.

    A file mounted into a container has to exist first, or docker makes a
    directory in its place and opencode finds something it cannot read.
    """
    path = project.opencode_auth_path
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        # 0600 from the moment it exists, rather than chmodded afterwards.
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w") as handle:
            handle.write(EMPTY)
    return path
