"""The MessagePack websocket protocol: authentication and command dispatch.

A client authenticates, then sends `{"action": "status", ...}`. The action names
a handler registered with @ws_handler; nothing else can be reached, so the
surface is exactly what has been written here.

The socket carries the whole control plane, so it is guarded by the runtime
secret rather than by network position: the port binds every interface, because
the hub's caddy has to reach it from inside docker.
"""

from __future__ import annotations

import hmac
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import msgpack

from ..driver.process import Event, EventStream
from ..driver.transcript import transcript
from ..project import Project

if TYPE_CHECKING:  # a cycle otherwise: sessions needs Clients from here
    from .sessions import Sessions

Handler = Callable[["WsCommand"], Awaitable[None]]

# action name -> handler. Populated by @ws_handler at import time.
COMMANDS: dict[str, Handler] = {}

AUTH_ACTION = "auth"


def pack_message(message: dict[str, Any]) -> bytes:
    """Encode one websocket message as a binary MessagePack frame."""
    return msgpack.packb(message, use_bin_type=True)


def unpack_message(frame: bytes) -> dict[str, Any]:
    """Decode one binary MessagePack frame and require a map at its root."""
    if not isinstance(frame, bytes):
        raise TypeError("websocket messages must be binary MessagePack frames")
    message = msgpack.unpackb(frame, raw=False)
    if not isinstance(message, dict):
        raise TypeError("websocket messages must contain a MessagePack map")
    return message


async def send_message(socket: Any, message: dict[str, Any]) -> None:
    """Send one websocket message using the protocol's binary wire format."""
    await socket.send(pack_message(message))


class Clients:
    """Every authenticated socket, so a run can be watched from several tabs."""

    def __init__(self) -> None:
        self._sockets: set[Any] = set()

    def add(self, socket: Any) -> None:
        self._sockets.add(socket)

    def discard(self, socket: Any) -> None:
        self._sockets.discard(socket)

    async def send(self, event: Event) -> None:
        """Send to everyone, dropping any socket that has gone away."""
        for socket in list(self._sockets):
            try:
                await send_message(socket, event)
            except Exception:
                self._sockets.discard(socket)

    async def close_all(self, code: int = 1001) -> None:
        """Hang up on every client, so the ui says so rather than going quiet.

        1001 is "going away", which is exactly what happened. Failures are
        ignored: a socket that cannot be closed is one that is already gone.
        """
        for socket in list(self._sockets):
            self._sockets.discard(socket)
            try:
                await socket.close(code)
            except Exception:
                pass


@dataclass(frozen=True)
class Context:
    """What every handler gets to work with."""

    project: Project
    clients: Clients
    sessions: "Sessions"


@dataclass(frozen=True)
class WsCommand:
    """One `{"action": ...}` message, with the socket it arrived on."""

    action: str
    #: Everything else the client sent, for handlers that take arguments.
    data: dict[str, Any]
    #: Anything with an async send(bytes) -- quart's websocket satisfies it.
    socket: Any
    context: Context
    #: Ties every event of this command together, so a client can keep
    #: concurrent commands apart on a shared socket.
    id: str = field(default_factory=lambda: uuid4().hex[:8])

    @classmethod
    def from_message(cls, message: dict[str, Any], socket: Any, context: Context) -> WsCommand:
        data = {k: v for k, v in message.items() if k not in ("action", "id")}
        offered_id = message.get("id")
        command_id = (
            offered_id
            if isinstance(offered_id, str) and 0 < len(offered_id) <= 64
            else uuid4().hex[:8]
        )
        return cls(
            action=message.get("action", ""),
            data=data,
            socket=socket,
            context=context,
            id=command_id,
        )

    def _stamp(self, event: Event) -> Event:
        return {**event, "id": self.id, "action": self.action}

    async def send(self, event: Event) -> None:
        """Send one event back to the client that asked."""
        await send_message(self.socket, self._stamp(event))

    async def publish(self, event: Event) -> None:
        """Send one event to every open client."""
        await self.context.clients.send(self._stamp(event))

    def response_stream(self) -> EventStream:
        """A stream for output only the caller cares about."""
        return self.send

    def broadcast_stream(self) -> EventStream:
        """A stream for output every open tab should see, like an agent run."""
        return self.publish

    @asynccontextmanager
    async def recorded(self, stream: EventStream) -> AsyncIterator[EventStream]:
        """`stream`, with a transcript of the same run kept on disk.

        The command's own action and id name the file, so a run in the ui and
        a file under `state/process_logs/` can be matched up afterwards.
        """
        path = self.project.process_log(self.action or "command", self.id)
        async with transcript(path, stream) as recording:
            yield recording

    @property
    def project(self) -> Project:
        return self.context.project

    @property
    def sessions(self) -> "Sessions":
        return self.context.sessions


def ws_handler(fn: Handler | None = None, *, name: str | None = None) -> Any:
    """Register a websocket handler, as `@ws_handler` or `@ws_handler(name=...)`.

    The name defaults to the function's, which is what the client puts in
    `action`.
    """

    def register(fn: Handler) -> Handler:
        COMMANDS[name or fn.__name__] = fn
        return fn

    return register if fn is None else register(fn)


def authenticate(message: dict[str, Any], secret: str) -> bool:
    """True if `message` is a valid opening `auth` frame."""
    if message.get("action") != AUTH_ACTION:
        return False
    offered = message.get("secret")
    return isinstance(offered, str) and hmac.compare_digest(offered, secret)


async def dispatch(message: dict[str, Any], socket: Any, context: Context) -> None:
    """Route one client message to its handler, reporting failures over the socket."""
    command = WsCommand.from_message(message, socket, context)
    handler = COMMANDS.get(command.action)

    if handler is None:
        await command.send({"type": "error", "message": f"unknown action {command.action!r}"})
        return

    try:
        await handler(command)
    except Exception as exc:  # a bad handler shouldn't take the socket down
        await command.send({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
