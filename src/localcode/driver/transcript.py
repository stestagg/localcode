"""Keeping what a process said, as well as showing it.

`driver.process` streams events to whoever is listening and forgets them. That
is right for the socket -- a tab that was not open missed it -- but wrong for
the machine: when something goes wrong in a container the output is the only
evidence there is, and by then every tab has been closed.

So this wraps a stream rather than replacing it. Everything still reaches the
browser exactly as before, and a plain transcript of the same run lands on disk
where a person can `cat` it. Deliberately not the event stream in another
format: the file is for reading, and the ui already has the structured version.
"""

from __future__ import annotations

import os
import shlex
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import IO, Any

from .process import Event, EventStream


def render(event: Event) -> str:
    """One event as the line a person would want to read, or "" for silence."""
    match event.get("type"):
        case "start":
            return f"$ {shlex.join(str(part) for part in event.get('argv', []))}\n"
        case "stdout" | "stderr":
            # Verbatim, interleaved as it arrived. Chunks are not lines, so
            # nothing is added here that the process did not write itself.
            return str(event.get("data", ""))
        case "error":
            return f"error: {event.get('message', '')}\n"
        case "exit":
            return f"exit {event.get('code')}\n"
        case _:
            return ""


@asynccontextmanager
async def transcript(path: Path, stream: EventStream) -> AsyncIterator[EventStream]:
    """`stream`, with everything also written to `path` as plain text.

    0600 and unbuffered: a docker argv carries the agent's gitea token and the
    project's provider credentials, and a run that is still going should be
    `tail -f`-able while it goes.

    Failing to write is never allowed to fail the run -- the log is a record of
    the work, not the work -- so a broken file is reported once on the stream
    and then dropped.
    """
    handle = _open(path)
    if handle is None:
        yield stream
        return

    broken = False

    async def record(event: Event) -> None:
        nonlocal broken
        await stream(event)
        if broken:
            return
        try:
            handle.write(render(event))
        except OSError as exc:
            broken = True
            await stream({"type": "stderr", "data": f"localcode: log {path}: {exc}\n"})

    try:
        yield record
    finally:
        handle.close()


def _open(path: Path) -> IO[Any] | None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    except OSError:
        # Nowhere to write is a reason to run without a log, not a reason not
        # to run. Nothing has been streamed yet, so there is nobody to tell.
        return None
    return os.fdopen(fd, "w", buffering=1)
