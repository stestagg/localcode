"""Git, on the host checkout.

The controller edits and commits the working tree, then pushes to the bare
master inside `.localcode/state/`. That master is a plain path on the same
filesystem, so a push is a local ref update -- no network, no credentials.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

MAIN_BRANCH = "main"
LOCALCODE_BRANCH = "localcode"


class GitError(Exception):
    pass


def git(*args: str, cwd: Path) -> str:
    """Run one git command, returning its stdout."""
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise GitError(f"git {' '.join(args)}: {result.stderr.strip()}")
    return result.stdout.strip()


def is_repo(path: Path) -> bool:
    return (path / ".git").is_dir()


def init(path: Path) -> None:
    """A repo with `main` as its initial branch, matching gitea's default."""
    git("init", "--initial-branch=main", str(path), cwd=path.parent)


def clone(url: str, path: Path) -> None:
    git("clone", url, str(path), cwd=path.parent)


def has_commits(path: Path) -> bool:
    try:
        git("rev-parse", "HEAD", cwd=path)
    except GitError:
        return False
    return True


def commit_paths(path: Path, message: str, *paths: str) -> None:
    """Commit only ``paths``, leaving every unrelated edit alone."""
    git("add", "-A", "--", *paths, cwd=path)
    staged = git("diff", "--cached", "--name-only", "--", *paths, cwd=path)
    if not staged:
        return
    git("commit", "-m", message, cwd=path)


def current_branch(path: Path) -> str:
    return git("branch", "--show-current", cwd=path)


def branch_exists(path: Path, branch: str) -> bool:
    try:
        git("show-ref", "--verify", "--quiet", f"refs/heads/{branch}", cwd=path)
    except GitError:
        return False
    return True


def tree_exists(path: Path, treeish: str) -> bool:
    try:
        git("cat-file", "-e", treeish, cwd=path)
    except GitError:
        return False
    return True


def ref_exists(path: Path, ref: str) -> bool:
    try:
        git("show-ref", "--verify", "--quiet", ref, cwd=path)
    except GitError:
        return False
    return True


def is_ancestor(path: Path, older: str, newer: str) -> bool:
    try:
        git("merge-base", "--is-ancestor", older, newer, cwd=path)
    except GitError:
        return False
    return True


def root(path: Path) -> Path | None:
    """Return the containing repository root, if ``path`` is inside one."""
    try:
        return Path(git("rev-parse", "--show-toplevel", cwd=path)).resolve()
    except GitError:
        return None


def prepare_localcode_branch(path: Path) -> None:
    """Check out the private metadata branch, rebased onto current ``main``.

    A dirty working tree is allowed when first switching branches so `init`
    does not capture or disturb a user's unrelated work. Rebasing is deferred
    until that work is clean.
    """
    if not has_commits(path):
        # Both branches need a real common base. In a newly initialized repo an
        # empty main commit is the only thing that belongs on main.
        git("commit", "--allow-empty", "-m", "Initial commit", cwd=path)

    if branch_exists(path, LOCALCODE_BRANCH):
        if current_branch(path) != LOCALCODE_BRANCH:
            git("switch", LOCALCODE_BRANCH, cwd=path)
    else:
        git("switch", "-c", LOCALCODE_BRANCH, MAIN_BRANCH, cwd=path)

    if not git("status", "--porcelain", cwd=path):
        rebase_localcode(path)


def rebase_localcode(path: Path) -> None:
    """Rebase ``localcode`` on ``main``, keeping its `.localcode` snapshot.

    The restore after rebase also removes `.localcode` paths accidentally added
    on main, so main can never replace or augment the metadata branch's copy.
    """
    if current_branch(path) != LOCALCODE_BRANCH:
        raise GitError(f"{LOCALCODE_BRANCH} must be checked out before rebasing")
    if not branch_exists(path, MAIN_BRANCH):
        return
    if git("status", "--porcelain", cwd=path):
        raise GitError("cannot rebase localcode with uncommitted changes")

    snapshot = git("rev-parse", f"refs/heads/{LOCALCODE_BRANCH}", cwd=path)
    try:
        git("rebase", "-X", "theirs", MAIN_BRANCH, cwd=path)
    except GitError:
        # Never strand the user's checkout halfway through an automatic sync.
        git("rebase", "--abort", cwd=path)
        raise
    # Clear the rebased copy first, then restore the old snapshot. This makes
    # the directory exact, including deletion of paths introduced by main.
    git("rm", "-r", "-f", "--ignore-unmatch", "--", ".localcode", cwd=path)
    if tree_exists(path, f"{snapshot}:.localcode"):
        git("read-tree", f"--prefix=.localcode/", f"{snapshot}:.localcode", cwd=path)
    git("checkout-index", "-f", "-a", cwd=path)
    staged = git("diff", "--cached", "--name-only", "--", ".localcode", cwd=path)
    if staged:
        git("commit", "-m", "Keep localcode metadata separate from main", cwd=path)


def sync_from_remote(path: Path, remote: str) -> None:
    """Absorb a subordinate Gitea mirror into the authoritative checkout.

    Agents can advance both mirror refs while localcode is running. Main is
    only fast-forwarded; real divergence needs a human decision. Metadata is
    integrated and then rebased onto the resulting authoritative main.
    """
    if current_branch(path) != LOCALCODE_BRANCH:
        raise GitError(f"{LOCALCODE_BRANCH} must be checked out before syncing")
    if git("status", "--porcelain", cwd=path):
        raise GitError("cannot sync localcode with uncommitted changes")

    git("fetch", "--prune", remote, cwd=path)

    local_main_ref = f"refs/heads/{MAIN_BRANCH}"
    remote_main = f"{remote}/{MAIN_BRANCH}"
    remote_main_ref = f"refs/remotes/{remote_main}"
    if ref_exists(path, remote_main_ref):
        if is_ancestor(path, local_main_ref, remote_main_ref):
            git("branch", "-f", MAIN_BRANCH, remote_main, cwd=path)
        elif not is_ancestor(path, remote_main_ref, local_main_ref):
            raise GitError(
                f"{MAIN_BRANCH} has diverged from {remote_main}; reconcile main "
                "in the host checkout before running localcode"
            )

    local_metadata_ref = f"refs/heads/{LOCALCODE_BRANCH}"
    remote_metadata = f"{remote}/{LOCALCODE_BRANCH}"
    remote_metadata_ref = f"refs/remotes/{remote_metadata}"
    if ref_exists(path, remote_metadata_ref):
        if is_ancestor(path, local_metadata_ref, remote_metadata_ref):
            git("merge", "--ff-only", remote_metadata, cwd=path)
        elif not is_ancestor(path, remote_metadata_ref, local_metadata_ref):
            try:
                # During rebase, "theirs" is the host commit being replayed:
                # deliberate authoritative edits win same-line conflicts.
                git("rebase", "-X", "theirs", remote_metadata, cwd=path)
            except GitError:
                git("rebase", "--abort", cwd=path)
                raise

    rebase_localcode(path)


def update_mirror(path: Path, remote: str) -> None:
    """Reconcile a Gitea mirror, then publish authoritative host refs.

    Rebasing metadata onto main rewrites its commit IDs, so only `localcode`
    gets a lease-protected force update. Main is always a normal push.
    """
    sync_from_remote(path, remote)
    push(path, remote, MAIN_BRANCH)
    push(path, remote, "--force-with-lease", LOCALCODE_BRANCH)


def set_remote(path: Path, name: str, url: str) -> None:
    """Point `name` at `url`, whether or not the remote already exists."""
    remotes = git("remote", cwd=path).split()
    verb = "set-url" if name in remotes else "add"
    git("remote", verb, name, url, cwd=path)


def push(path: Path, remote: str, *refspecs: str) -> None:
    git("push", remote, *refspecs, cwd=path)


def branches(path: Path) -> list[str]:
    listing = git("for-each-ref", "--format=%(refname:short)", "refs/heads", cwd=path)
    return listing.split("\n") if listing else []


def head_summary(path: Path) -> str:
    """`<short sha> <subject>` for the current HEAD, or "" in an empty repo."""
    if not has_commits(path):
        return ""
    return git("log", "-1", "--format=%h %s", cwd=path)


def file_at_ref(path: Path, ref: str, filename: str) -> str | None:
    """Return a committed text file, or ``None`` when it is absent.

    Reading through git rather than the working tree matters for localcode: the
    checkout normally sits on the private metadata branch while project files
    shown in the UI must come from ``main``.
    """
    try:
        return git("show", f"{ref}:{filename}", cwd=path)
    except GitError:
        return None
