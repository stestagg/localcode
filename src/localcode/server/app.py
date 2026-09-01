"""The controller's websocket server.

There is nothing else here: pages, static assets and gitea all come from the hub
container. This process exists to hold the project, authenticate clients and
drive docker on their behalf.
"""

from __future__ import annotations

import asyncio

from quart import Quart, websocket

from ..project import Project
from . import commands  # noqa: F401  -- imported for its @ws_handler registrations
from .sessions import Sessions
from .ws import Clients, Context, authenticate, dispatch, send_message, unpack_message


def create_app(
    project: Project,
    clients: Clients,
    stop: asyncio.Event | None = None,
    sessions: Sessions | None = None,
) -> Quart:
    app = Quart(__name__)
    sessions = Sessions(project) if sessions is None else sessions
    context = Context(project=project, clients=clients, sessions=sessions)
    # Handlers have to let go of their sockets themselves. Hypercorn's graceful
    # shutdown closes the listening socket and then waits for every open
    # connection, and a websocket parked in receive() is never idle, so nothing
    # else will ever hang up on it: without this the server task never returns.
    stop = asyncio.Event() if stop is None else stop

    @app.websocket("/ws")
    async def ws() -> None:
        await websocket.accept()
        closing = asyncio.ensure_future(stop.wait())

        async def receive() -> str | bytes | None:
            """The next frame, or None once a shutdown has been asked for."""
            incoming = asyncio.ensure_future(websocket.receive())
            await asyncio.wait({incoming, closing}, return_when=asyncio.FIRST_COMPLETED)
            if incoming.done():
                return incoming.result()
            incoming.cancel()
            return None

        # Dispatched as tasks rather than awaited: an agent run takes minutes,
        # and the socket has to stay answerable while it does.
        running: set[asyncio.Task] = set()
        socket = websocket._get_current_object()
        try:
            opening = await receive()
            # A socket that has not said anything yet still holds shutdown up,
            # so the guard has to cover the opening frame as much as the rest.
            if opening is None:
                return
            if not authenticate(unpack_message(opening), project.runtime.secret):
                await send_message(websocket, {"type": "error", "message": "unauthorised"})
                return

            clients.add(socket)
            await send_message(websocket, {"type": "ready"})

            while True:
                frame = await receive()
                if frame is None:
                    # Shutting down. Returning ends the handler, which is what
                    # makes quart send the close frame and hypercorn let go.
                    return
                task = asyncio.create_task(dispatch(unpack_message(frame), socket, context))
                running.add(task)
                task.add_done_callback(running.discard)
        finally:
            closing.cancel()
            clients.discard(socket)
            # Whatever this socket was watching or working on, it is not any
            # more. A session holding a dead socket keeps sending into it.
            sessions.discard(socket)
            for task in running:
                task.cancel()

    return app
