"""Watch committed localcode metadata and tell connected clients when it changes."""

from __future__ import annotations

import asyncio
import hashlib
import os
from collections.abc import Awaitable, Callable
from pathlib import Path

from .. import repo as git
from ..project import Project
from .ws import Clients

SyncMetadata = Callable[[Project], Awaitable[None]]
RemoteRevision = Callable[[Project], str]


def metadata_revision(project: Project) -> str:
    """A stable revision of metadata files plus the local repository refs.

    Runtime state is deliberately excluded: Gitea's database and logs change
    constantly and are not project configuration. Both HEAD and main are
    included so repository commits and metadata commits are detectable.
    """
    digest = hashlib.sha256()
    # Track both the checked-out metadata commit and main independently. Main
    # can be fast-forwarded without moving HEAD while localcode is running.
    for ref in ("HEAD", f"refs/heads/{git.MAIN_BRANCH}"):
        try:
            digest.update(git.git("rev-parse", ref, cwd=project.path).encode())
        except git.GitError:
            pass

    if not project.localcode_dir.exists():
        return digest.hexdigest()

    for path in _metadata_files(project):
        digest.update(path.relative_to(project.localcode_dir).as_posix().encode())
        try:
            digest.update(path.read_bytes())
        except OSError:
            # A file can disappear between the walk and the read during an
            # editor's atomic save. The next poll observes the settled tree.
            continue
    return digest.hexdigest()


def _metadata_files(project: Project) -> list[Path]:
    """Every configuration file under `.localcode/`, in a stable order.

    `state/` is pruned rather than walked and discarded. It holds gitea's
    repositories, database, logs and sessions, which grow without bound while a
    project runs -- descending into it makes this poll, which happens once a
    second, steadily more expensive the longer localcode has been up.
    """
    found: list[Path] = []
    for directory, subdirectories, names in os.walk(project.localcode_dir):
        root = Path(directory)
        if root == project.localcode_dir:
            subdirectories[:] = [name for name in subdirectories if name != "state"]
        subdirectories.sort()
        for name in sorted(names):
            path = root / name
            if path.is_file():
                found.append(path)
    return found


def remote_metadata_revision(project: Project) -> str:
    """The main and metadata commits currently published by the Gitea mirror."""
    try:
        output = git.git(
            "ls-remote",
            "--refs",
            "localcode",
            f"refs/heads/{git.MAIN_BRANCH}",
            f"refs/heads/{git.LOCALCODE_BRANCH}",
            cwd=project.path,
        )
    except git.GitError:
        return ""
    return output


def observed_revision(
    project: Project,
    remote_revision: RemoteRevision = remote_metadata_revision,
) -> tuple[str, str]:
    """Both authoritative copies whose divergence should trigger a sync."""
    return metadata_revision(project), remote_revision(project)


async def _observe(
    project: Project,
    remote_revision: RemoteRevision,
) -> tuple[str, str]:
    """`observed_revision`, off the event loop.

    Both halves shell out to git synchronously, and `ls-remote` goes over http
    to gitea -- a hundred milliseconds and rising, every second. Run on the
    loop it holds up everything else the controller has to answer, Ctrl-C
    included, which is why it goes to a thread.
    """
    return await asyncio.to_thread(observed_revision, project, remote_revision)


async def watch_metadata(
    project: Project,
    clients: Clients,
    stop: asyncio.Event,
    sync: SyncMetadata,
    *,
    interval: float = 1.0,
    remote_revision: RemoteRevision = remote_metadata_revision,
) -> None:
    """Poll host and Gitea metadata, reconcile changes, and broadcast invalidation."""
    revision = await _observe(project, remote_revision)
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
            continue
        except TimeoutError:
            pass

        changed = await _observe(project, remote_revision)
        if changed == revision:
            continue
        revision = changed

        warning = ""
        try:
            await sync(project)
            # Sync can fetch, merge, rebase, and push. Record its settled state
            # so those expected changes do not cause a duplicate notification.
            revision = await _observe(project, remote_revision)
        except Exception as exc:
            # Dirty/incomplete edits should refresh the UI but should not be
            # pushed or take down the controller. A later commit retries sync.
            warning = f"{type(exc).__name__}: {exc}"

        await clients.send(
            {
                "type": "metadata.changed",
                **({"message": warning} if warning else {}),
            }
        )
