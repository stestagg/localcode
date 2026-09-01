"""`localcode run`: bring a project up, keep it up, and take it down again.

The controller lives out here on the host. It owns the project, serves the
websocket the ui drives everything through, and spawns the containers -- one
hub that stays up, and an agent per piece of work that does not.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import signal
import socket
import sys
import time
import webbrowser
from collections.abc import Awaitable
from typing import Any

from hypercorn.asyncio import serve
from hypercorn.config import Config

from .docker import agent, client
from .docker.hub import Hub
from .driver.process import Event
from .gitea import setup
from .project import DEFAULT_HTTP_PORT, Project
from .server.app import create_app
from .server.watch import watch_metadata
from .server.sessions import Sessions
from .server.ws import Clients


class Stopwatch:
    """Time since the stop was asked for, so every line below says when."""

    def __init__(self) -> None:
        self._started: float | None = None

    def start(self) -> None:
        if self._started is None:
            self._started = time.monotonic()

    @property
    def elapsed(self) -> float:
        return 0.0 if self._started is None else time.monotonic() - self._started


CLOCK = Stopwatch()


def say(message: str) -> None:
    """One line of shutdown narration, stamped and flushed immediately.

    Shutdown is the one place where output that arrives late is worse than no
    output at all, so nothing here is buffered and nothing waits for the step
    it describes to finish.
    """
    print(f"localcode: [{CLOCK.elapsed:6.2f}s] {message}", file=sys.stderr, flush=True)


async def _tick(name: str, begun: float) -> None:
    """Say that a step is still going, until it is not.

    Every second at first, then every five: close attention while a step could
    still be about to finish, and a heartbeat once it is clearly stuck.

    Silence from this is itself a reading. The ticker is a task like any other,
    so if it stops reporting, the event loop is blocked rather than merely busy
    -- and a blocked loop is a Ctrl-C that cannot be answered.
    """
    while True:
        elapsed = time.monotonic() - begun
        await asyncio.sleep(1.0 if elapsed < 5 else 5.0)
        say(f"  ... {name}: still going after {time.monotonic() - begun:.1f}s")


async def stage(name: str, work: Awaitable[Any], *, timeout: float) -> bool:
    """Run one shutdown step: narrated, time-boxed, and never fatal.

    Every step is bounded on its own rather than sharing one budget, so a step
    that hangs costs its own timeout and the ones after it still run.
    """
    say(f"{name}: starting")
    begun = time.monotonic()
    ticker = asyncio.ensure_future(_tick(name, begun))
    try:
        await asyncio.wait_for(work, timeout)
    except TimeoutError:
        say(f"{name}: GAVE UP after {timeout:.0f}s")
        return False
    except asyncio.CancelledError:
        if not (isinstance(work, asyncio.Task) and work.cancelled()):
            raise  # this shutdown is itself being cancelled; do not swallow it
        say(f"{name}: cancelled after {time.monotonic() - begun:.1f}s")
        return True
    except Exception as exc:
        say(f"{name}: FAILED after {time.monotonic() - begun:.1f}s -- {type(exc).__name__}: {exc}")
        return False
    else:
        say(f"{name}: done in {time.monotonic() - begun:.1f}s")
        return True
    finally:
        ticker.cancel()


async def loop_watchdog(threshold: float = 0.4, interval: float = 0.25) -> None:
    """Complain when the event loop stalls.

    A blocked loop cannot run the signal handler either, so a stall here is
    exactly the window in which a Ctrl-C looks like it was ignored. Naming the
    stall is the difference between "it hung" and "it hung in git".
    """
    while True:
        before = time.monotonic()
        await asyncio.sleep(interval)
        lag = time.monotonic() - before - interval
        if lag > threshold:
            print(
                f"localcode: warning: event loop blocked for {lag:.1f}s "
                "(a Ctrl-C arriving now would wait that long)",
                file=sys.stderr,
                flush=True,
            )


async def console(event: Event) -> None:
    """An event stream that writes to this terminal, for start-up output."""
    if event["type"] in ("stdout", "stderr"):
        sys.stderr.write(event["data"])
        sys.stderr.flush()
    elif event["type"] == "error":
        print(f"localcode: {event['message']}", file=sys.stderr)


def free_port() -> int:
    """A port nothing is listening on, for the controller's socket."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class Interrupted(Exception):
    """Raised in place of whatever was being awaited when a stop was asked for."""


class Shutdown:
    """Ctrl-C once to stop; Ctrl-C again to stop waiting for the stopping.

    The handler goes in before the first slow thing happens. Image builds and
    gitea's first migration are the longest waits there are, and an interrupt
    that only takes effect once they finish is an interrupt that looks ignored.
    """

    def __init__(self) -> None:
        self.requested = asyncio.Event()
        self._presses = 0

    def install(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self._press)

    def _press(self) -> None:
        self._presses += 1
        if self._presses == 1:
            CLOCK.start()
            print(
                "\nlocalcode: stopping (Ctrl-C again to exit without waiting)",
                file=sys.stderr,
                flush=True,
            )
            self.requested.set()
            return

        say("second Ctrl-C: exiting now; containers are still running")
        print(
            "localcode: docker rm -f $(docker ps -q --filter label=localcode.project)",
            file=sys.stderr,
            flush=True,
        )
        # There is nothing left to unwind that matters more than answering the
        # key press, so do not give the loop the chance to keep us here.
        os._exit(130)

    async def guard(self, awaitable: Awaitable[Any]) -> Any:
        """Await something, abandoning it if a stop is asked for meanwhile."""
        task = asyncio.ensure_future(awaitable)
        waiter = asyncio.ensure_future(self.requested.wait())
        try:
            await asyncio.wait({task, waiter}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            waiter.cancel()

        if not task.done():
            task.cancel()
            raise Interrupted
        return task.result()


async def _serve_ws(
    project: Project, clients: Clients, sessions: Sessions, stop: asyncio.Event
) -> None:
    config = Config()
    # Every interface, not loopback: caddy reaches this from inside docker, over
    # the host gateway, and so does an agent container reporting into a session.
    # The runtime secret is what makes that safe.
    config.bind = [f"0.0.0.0:{project.runtime.ws_port}"]
    config.accesslog = None
    config.errorlog = None
    await serve(
        create_app(project, clients, stop, sessions), config, shutdown_trigger=stop.wait
    )


async def _clean_up(
    server: asyncio.Task | None,
    watcher: asyncio.Task | None,
    clients: Clients | None,
    sessions: Sessions | None,
    project: Project,
    hub: Hub,
) -> None:
    """Take everything down, saying what is being taken down while it happens.

    Each step is separately bounded, so one that will not finish is reported by
    name and costs only its own timeout. Nothing here raises: a failed step is
    narrated and the rest still run.
    """
    say("shutting down")
    done = True

    if clients is not None:
        done &= await stage(
            "closing browser connections", clients.close_all(), timeout=5
        )
    if server is not None:
        done &= await stage("stopping the websocket server", server, timeout=10)
    if watcher is not None:
        done &= await stage("stopping the metadata watcher", watcher, timeout=15)
    if sessions is not None:
        # Before the containers, so each run gets to write out whatever its
        # session had collected rather than being cut off mid-answer.
        done &= await stage("stopping agent sessions", sessions.shutdown(), timeout=15)

    done &= await stage("stopping agent containers", agent.stop_all(project), timeout=30)
    done &= await stage("stopping the hub container", hub.stop(), timeout=30)
    done &= await stage(
        "removing the docker network", client.network_remove(project.network), timeout=15
    )

    if done:
        say("shutdown complete")
    else:
        _orphaned("shutdown did not finish cleanly; containers may still be running")


def _orphaned(reason: str) -> None:
    say(reason)
    print(
        "localcode: docker rm -f $(docker ps -q --filter label=localcode.project)",
        file=sys.stderr,
        flush=True,
    )


async def run(
    project: Project,
    *,
    dev: bool = False,
    rebuild: bool = False,
    browser: bool = True,
    port: int | None = None,
) -> int:
    # Before anything slow, so the first Ctrl-C is always the one that counts.
    shutdown = Shutdown()
    shutdown.install()

    if not await client.available():
        print("localcode: docker is not running", file=sys.stderr)
        return 1

    for issue in project.persona_issues():
        print(
            f"localcode: warning: personas/{issue.path.name}: {issue.message}",
            file=sys.stderr,
        )
    for issue in project.role_issues():
        print(
            f"localcode: warning: roles/{issue.path.name}: {issue.message}",
            file=sys.stderr,
        )

    project.runtime.secret = secrets.token_urlsafe(32)
    project.runtime.http_port = port or DEFAULT_HTTP_PORT
    project.runtime.ws_port = free_port()
    project.runtime.pid = os.getpid()
    project.state_dir.mkdir(parents=True, exist_ok=True)
    project.save_runtime()

    setup.write_config(project)

    hub = Hub(project, dev=dev)
    server: asyncio.Task | None = None
    watcher: asyncio.Task | None = None
    clients: Clients | None = None
    sessions: Sessions | None = None
    code = 0

    # Runs for the whole session: the stalls it reports during a run are the
    # same stalls that make a Ctrl-C feel ignored.
    lag = asyncio.ensure_future(loop_watchdog())

    try:
        await shutdown.guard(hub.ensure_image(console, rebuild=rebuild))
        await shutdown.guard(agent.ensure_image(console, rebuild=rebuild))

        # The socket comes up before the hub: caddy proxies to it, and the ui
        # connects the moment the page loads.
        clients = Clients()
        sessions = Sessions(project)
        server = asyncio.create_task(
            _serve_ws(project, clients, sessions, shutdown.requested)
        )

        await shutdown.guard(hub.start(project.runtime.ws_port))
        await shutdown.guard(setup.provision(project))
        project.save_runtime()
        watcher = asyncio.create_task(
            watch_metadata(
                project,
                clients,
                shutdown.requested,
                setup.sync_metadata,
            )
        )

        print(f"localcode: {project.name} on {project.http_url}", file=sys.stderr)
        print(f"localcode: gitea on {project.gitea_url}", file=sys.stderr)
        print(
            f"localcode: gitea sign-in {setup.HUMAN_USER} / "
            f"{project.runtime.human_password}",
            file=sys.stderr,
        )
        if browser:
            webbrowser.open(project.http_url)

        await shutdown.requested.wait()
    except Interrupted:
        code = 130
    finally:
        CLOCK.start()
        shutdown.requested.set()
        # The watchdog stays up through cleanup: a stall during shutdown is the
        # one worth naming.
        await _clean_up(server, watcher, clients, sessions, project, hub)
        lag.cancel()

    return code
