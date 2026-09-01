"""Creating `.localcode/` in a repo, for `localcode init` and `localcode clone`."""

from __future__ import annotations

import shutil
from importlib import resources
from pathlib import Path

from . import repo
from .project import LOCALCODE_DIR, Project


def _copy_missing(source: Path, target: Path) -> None:
    """Copy everything under `source` that `target` does not already have.

    Never overwrites: a repo that has been through this before keeps its
    personas, roles and whatever else has been edited since.
    """
    target.mkdir(parents=True, exist_ok=True)
    for item in sorted(source.rglob("*")):
        destination = target / item.relative_to(source)
        if item.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
        elif not destination.exists():
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, destination)


def scaffold(path: Path) -> Project:
    """Lay `.localcode/` into `path`, filling in only what is missing.

    Returns the project, ready to run.
    """
    path = path.resolve()
    if not repo.is_repo(path):
        repo.init(path)

    repo.prepare_localcode_branch(path)

    target = path / LOCALCODE_DIR
    template = resources.files("localcode") / "templates" / "localcode"

    with resources.as_file(template) as source:
        _copy_missing(Path(source), target)

    # This used to hold only values now derived from the path or selected at
    # runtime. Remove it when an existing project is scaffolded again.
    (target / "config.toml").unlink(missing_ok=True)

    (target / "state").mkdir(exist_ok=True)

    repo.commit_paths(path, "Set up localcode", LOCALCODE_DIR)
    return Project(path=path)
