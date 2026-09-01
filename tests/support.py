"""Project fixtures shared by tests that exercise orchestration boundaries."""

from __future__ import annotations

import tempfile
from collections.abc import Mapping
from pathlib import Path
from unittest import TestCase

from localcode.project import Project, Runtime


def make_project(
    case: TestCase,
    *,
    personas: Mapping[str, str] | None = None,
    roles: Mapping[str, str] | None = None,
    stories: Mapping[str, str] | None = None,
    runtime: Runtime | None = None,
) -> Project:
    """Create the metadata needed by a test and register its cleanup."""
    temporary = tempfile.TemporaryDirectory()
    case.addCleanup(temporary.cleanup)
    root = Path(temporary.name) / "demo"

    for directory, definitions in (("personas", personas or {}), ("roles", roles or {})):
        target = root / ".localcode" / directory
        target.mkdir(parents=True, exist_ok=True)
        for name, prompt in definitions.items():
            (target / f"{name}.md").write_text(prompt)

    for relative, source in (stories or {}).items():
        target = root / ".localcode/stories" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source)

    return Project(root, runtime or Runtime())
