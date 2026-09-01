"""Agent sessions: what an agent said, and what a person said back.

This is the layer above the process log. A process log is everything a
container emitted; a session is what the agent in it deliberately chose to
say -- attributed to a name, stamped with a time, and structured enough that
the ui can render it as a conversation rather than as a wall of stdout. The two
are kept apart on purpose: a container that dies before it ever opens a socket
leaves a full process log and an empty session, and that difference is the
diagnosis.

The channel runs both ways. Viewers can pause, resume, stop and type, and those
reach the agent as events on its own socket. Nothing here interrupts anything:
an agent observes them at checkpoints of its choosing, which is why a stop also
gets to hard-kill the container behind it.

Sessions are written to `state/sessions/<id>.jsonl` as they happen, one line per
message, so a reloaded tab -- or a restarted controller -- can replay one
instead of finding it gone.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import time
from collections import deque
from collections.abc import Coroutine
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from ..project import Project
from .ws import Clients

#: What a message carries. Images are here now because the wire is MessagePack
#: and adding them later would mean changing it.
TEXT = "text"
IMAGE = "image"
STATUS = "status"
KINDS = frozenset({TEXT, IMAGE, STATUS})

#: Where a session can be. The live two, then four ways of being over -- which
#: are kept apart because "it worked" is a claim, and nothing should make it on
#: a session's behalf. `FINISHED` is only ever set by something that watched the
#: work run out; anything that merely stopped watching says so instead.
RUNNING = "running"
PAUSED = "paused"
#: A person stopped it.
STOPPED = "stopped"
#: It ran to the end and said what it had to say.
FINISHED = "finished"
#: It ended badly -- a non-zero exit, or something raised.
FAILED = "failed"
#: Nobody was left to find out how it went: the controller went down, or the
#: run was cancelled out from under itself. Never a synonym for finished.
ABANDONED = "abandoned"
STATES = frozenset({RUNNING, PAUSED, STOPPED, FINISHED, FAILED, ABANDONED})

#: Whatever a viewer types is attributed to this, so a transcript reads as a
#: conversation rather than as a log with one anonymous line in it.
USER = "you"

#: A session id names a file, so it is held to more than the command ids in
#: `ws.py` are.
SESSION_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

#: Line kinds in the jsonl record that are not messages.
META = "meta"
STATE = "state"


class UnknownSession(Exception):
    pass


class SessionExists(Exception):
    pass


@dataclass(frozen=True)
class Message:
    """One thing an agent or a person said."""

    seq: int
    at: float
    agent: str
    kind: str
    text: str = ""
    mime: str = ""
    data: bytes = b""

    def wire(self) -> dict[str, Any]:
        """For MessagePack, which carries bytes natively."""
        body: dict[str, Any] = {
            "seq": self.seq,
            "at": self.at,
            "agent": self.agent,
            "kind": self.kind,
        }
        if self.text:
            body["text"] = self.text
        if self.kind == IMAGE:
            body["mime"] = self.mime
            body["data"] = self.data
        return body

    def record(self) -> dict[str, Any]:
        """For the jsonl, which cannot -- so an image is base64 there."""
        body = self.wire()
        if self.kind == IMAGE:
            body["data"] = base64.b64encode(self.data).decode()
        return body

    @classmethod
    def restore(cls, record: dict[str, Any]) -> Message:
        data = record.get("data", "")
        return cls(
            seq=int(record["seq"]),
            at=float(record.get("at", 0.0)),
            agent=str(record.get("agent", "")),
            kind=str(record.get("kind", TEXT)),
            text=str(record.get("text", "")),
            mime=str(record.get("mime", "")),
            data=base64.b64decode(data) if data else b"",
        )


@dataclass
class Session:
    """One conversation, live and on disk."""

    id: str
    title: str
    agent: str
    path: Path
    #: The workflow that owns this conversation. Older records predate the
    #: field and are restored as one-shot `ask` sessions.
    process: str = "ask"
    at: float = field(default_factory=time.time)
    state: str = RUNNING
    messages: list[Message] = field(default_factory=list)
    #: Browser tabs watching, and agent sockets working. Two sets rather than
    #: one because the events they get are not the same events.
    viewers: Clients = field(default_factory=Clients)
    workers: Clients = field(default_factory=Clients)
    #: User messages not yet collected by a worker. They keep their sequence
    #: number so a worker can deduplicate the pushed and catch-up forms.
    inputs: deque[Message] = field(default_factory=deque)
    #: What to kill if a stop is not honoured. None for a session nothing was
    #: started for.
    container: str | None = None
    #: Messages still being streamed, by the stream id their author gave
    #: them. An answer arrives a token at a time and is written once, at the
    #: end -- see `post`. Keyed by the author's own id rather than by seq so
    #: that seq stays this side's to assign, and stays sequential on disk.
    _open: dict[str, Message] = field(default_factory=dict)
    _next_seq: int = 0

    # --- reading -------------------------------------------------------------

    @property
    def live(self) -> bool:
        return self.state in (RUNNING, PAUSED)

    def summary(self) -> dict[str, Any]:
        return {
            "session": self.id,
            "title": self.title,
            "agent": self.agent,
            "process": self.process,
            "state": self.state,
            "at": self.at,
            "messages": len(self.messages),
        }

    def history(self) -> dict[str, Any]:
        return {
            "type": "session.history",
            **self.summary(),
            "messages": [message.wire() for message in self.messages],
        }

    # --- writing -------------------------------------------------------------

    def post(
        self,
        agent: str,
        kind: str,
        *,
        text: str = "",
        mime: str = "",
        data: bytes = b"",
        stream: str | None = None,
        done: bool = True,
    ) -> Message:
        """Append or extend one message.

        A streamed answer arrives as many calls with `done=False` sharing a
        `stream`: every one of them is broadcast, so the browser sees the text
        appear, and only the last writes a line. That is what keeps one answer
        one line in the record without giving up streaming.
        """
        if kind not in KINDS:
            raise ValueError(f"unknown message kind {kind!r}")

        open_message = None if stream is None else self._open.get(stream)
        if open_message is None:
            message = Message(
                seq=self._next_seq,
                at=time.time(),
                agent=agent,
                kind=kind,
                text=text,
                mime=mime,
                data=data,
            )
            self._next_seq += 1
            self.messages.append(message)
        else:
            # Extending one that is still streaming: the text grows, and
            # everything else was settled by the call that opened it.
            message = replace(open_message, text=open_message.text + text)
            self.messages[self._index(message.seq)] = message

        if stream is not None and not done:
            self._open[stream] = message
        elif stream is not None:
            self._open.pop(stream, None)

        if done:
            self._write(message.record())
        return message

    def flush(self) -> list[Message]:
        """Write out anything still streaming, for a run that ended abruptly."""
        pending = list(self._open.values())
        self._open.clear()
        for message in pending:
            self._write(message.record())
        return pending

    def set_state(self, state: str) -> None:
        if state not in STATES:
            raise ValueError(f"unknown session state {state!r}")
        if state == self.state:
            return
        self.state = state
        self._write({"kind": STATE, "at": time.time(), "state": state})

    def _index(self, seq: int) -> int:
        """Where `seq` sits. Searched from the end: a message that is still
        streaming is almost always the last one."""
        for index in range(len(self.messages) - 1, -1, -1):
            if self.messages[index].seq == seq:
                return index
        raise KeyError(seq)

    # --- fan-out -------------------------------------------------------------

    async def broadcast(self, event: dict[str, Any]) -> None:
        """To every tab watching."""
        await self.viewers.send({**event, "session": self.id})

    async def signal(self, event: dict[str, Any]) -> None:
        """To every agent working. Nothing acts on these until the agent looks."""
        await self.workers.send({**event, "session": self.id})

    def discard(self, socket: Any) -> None:
        self.viewers.discard(socket)
        self.workers.discard(socket)

    # --- the record ----------------------------------------------------------

    def _write(self, record: dict[str, Any]) -> None:
        """Append one line. A record that cannot be kept must not fail a run."""
        try:
            with self._append() as handle:
                handle.write(json.dumps(record, separators=(",", ":")) + "\n")
        except OSError:
            pass

    def _append(self) -> Any:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # 0600 from the moment it exists: a transcript is as private as the
        # work that produced it.
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        return os.fdopen(fd, "a")

    @classmethod
    def load(cls, path: Path) -> Session | None:
        """Rebuild a session from its record, replaying it in order.

        A half-written last line -- the controller was killed mid-append -- is
        dropped rather than raising: the rest of the conversation is still
        worth having.
        """
        try:
            lines = path.read_text().splitlines()
        except OSError:
            return None

        session: Session | None = None
        for line in lines:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind = record.get("kind")
            if kind == META:
                session = cls(
                    id=str(record.get("id", path.stem)),
                    title=str(record.get("title", "")),
                    agent=str(record.get("agent", "")),
                    path=path,
                    process=str(record.get("process", "ask")),
                    at=float(record.get("at", 0.0)),
                )
            elif session is None:
                continue
            elif kind == STATE:
                session.state = str(record.get("state", RUNNING))
            elif "seq" in record:
                message = Message.restore(record)
                session.messages.append(message)
                session._next_seq = max(session._next_seq, message.seq + 1)

        if session is not None and session.live:
            # It was still going when the record stopped growing, which means
            # the controller went down mid-run. That is not the same as having
            # finished, and must not be shown as though it were: nobody ever
            # saw how this one ended.
            session.state = ABANDONED
        return session


class Sessions:
    """Every session this project has, live in memory or waiting on disk."""

    def __init__(self, project: Project) -> None:
        self._project = project
        self._live: dict[str, Session] = {}
        #: Work still going, held here rather than by whoever asked for it.
        self._running: set[asyncio.Task] = set()

    def create(
        self,
        session_id: str | None,
        title: str,
        agent: str = "",
        process: str = "ask",
    ) -> Session:
        """A new session, under the id the client offered if it offered one.

        Letting the browser name it is what removes the race: it can render the
        viewer and subscribe before anything has been posted, the same way
        `WsCommand` lets a client name its own command.
        """
        chosen = session_id or os.urandom(6).hex()
        if session_id is not None and not SESSION_ID.match(session_id):
            raise ValueError(f"unusable session id {session_id!r}")
        if chosen in self._live or self._project.session_path(chosen).exists():
            raise SessionExists(f"session {chosen!r} already exists")

        session = Session(
            id=chosen,
            title=title,
            agent=agent,
            path=self._project.session_path(chosen),
            process=process,
        )
        session._write(
            {
                "kind": META,
                "id": session.id,
                "title": session.title,
                "agent": session.agent,
                "process": session.process,
                "at": session.at,
            }
        )
        self._live[chosen] = session
        return session

    def get(self, session_id: Any) -> Session:
        """The named session, loaded from disk if this process never had it."""
        if not isinstance(session_id, str) or not SESSION_ID.match(session_id):
            raise UnknownSession(f"no session {session_id!r}")
        if (session := self._live.get(session_id)) is not None:
            return session
        loaded = Session.load(self._project.session_path(session_id))
        if loaded is None:
            raise UnknownSession(f"no session {session_id!r}")
        self._live[session_id] = loaded
        return loaded

    def list(self, limit: int = 50) -> list[dict[str, Any]]:
        """The most recent sessions, newest first, without their messages."""
        found: dict[str, dict[str, Any]] = {
            session.id: session.summary() for session in self._live.values()
        }
        directory = self._project.sessions_dir
        if directory.is_dir():
            for path in sorted(
                directory.glob("*.jsonl"),
                key=lambda item: item.stat().st_mtime,
                reverse=True,
            )[: limit * 2]:
                if path.stem in found:
                    continue
                if (session := Session.load(path)) is not None:
                    found[session.id] = session.summary()

        return sorted(found.values(), key=lambda item: item["at"], reverse=True)[:limit]

    def start(self, work: Coroutine[Any, Any, None]) -> asyncio.Task:
        """Run `work` for as long as it takes, owned by nothing in particular.

        A session outlives the tab that started it. Handlers are dispatched as
        tasks tied to the socket they arrived on, and that socket is cancelled
        the moment the tab reloads or navigates away -- which would unwind the
        run's cleanup while its container carried on happily posting into a
        session already marked finished. So the work is handed over here, where
        the only thing that ends it is the controller shutting down.
        """
        task = asyncio.create_task(work)
        self._running.add(task)
        task.add_done_callback(self._running.discard)
        return task

    async def shutdown(self) -> None:
        """Stop every run still going, for the controller going down."""
        for task in list(self._running):
            task.cancel()
        if self._running:
            await asyncio.gather(*list(self._running), return_exceptions=True)

    def discard(self, socket: Any) -> None:
        """Forget one socket, wherever it was watching or working."""
        for session in self._live.values():
            session.discard(socket)
