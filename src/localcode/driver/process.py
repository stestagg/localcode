"""Running subprocesses with their output streamed somewhere live.

The webui hands in a stream that stamps events with a command id and forwards
them to a websocket; the main loop will hand in one that fans out to several.
Either way the driver stays free of any web framework, and a test can pass a
coroutine that appends to a list.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

# One JSON-serialisable event, and something that takes them. Events say what
# happened and nothing about who is listening -- attributing them to a
# particular run or socket is the stream's job.
Event = dict[str, Any]
EventStream = Callable[[Event], Awaitable[None]]

# Bytes per read. Chunks rather than readline(): a line longer than the stream
# limit makes readline() raise, and output without a trailing newline (progress
# bars, prompts) would sit in the buffer instead of reaching the browser.
_CHUNK = 4096


async def _pump(pipe: asyncio.StreamReader, kind: str, stream: EventStream) -> None:
    while chunk := await pipe.read(_CHUNK):
        await stream({"type": kind, "data": chunk.decode(errors="replace")})


async def run_command(
    argv: Sequence[str], stream: EventStream, *, stdin: str | None = None
) -> int:
    """Run `argv`, streaming its output. Returns the exit code.

    Emits `start`, then `stdout`/`stderr` as output arrives, then `exit`. A
    process that could not be started at all emits `error` and returns 127,
    the shell's convention for "command not found".

    `stdin` is written and the pipe closed before the output is drained, for
    input that is known up front -- a prompt, a patch. Something that has to be
    fed while the process is still talking needs a different function than this
    one; nothing here does.
    """
    argv = list(argv)
    await stream({"type": "start", "argv": argv})

    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE if stdin is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except (OSError, ValueError) as exc:
        await stream({"type": "error", "message": str(exc)})
        return 127

    if stdin is not None and proc.stdin is not None:
        proc.stdin.write(stdin.encode())
        # A process that never reads its stdin -- one that failed before it got
        # that far -- makes both of these raise rather than block. Its output
        # and its exit code are what the caller is here for, so drain on.
        try:
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        proc.stdin.close()

    # Both pipes have to be drained at once, or a process that fills one while
    # we read the other deadlocks.
    async with asyncio.TaskGroup() as tg:
        tg.create_task(_pump(proc.stdout, "stdout", stream))
        tg.create_task(_pump(proc.stderr, "stderr", stream))

    code = await proc.wait()
    await stream({"type": "exit", "code": code})
    return code
