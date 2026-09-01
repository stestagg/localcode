"""The terminal side of the LLM: `configure` and `ask`.

Configuring is a one-off, interactive, secret-handling job, which is why it is
a command out here rather than something the web ui does: the key should not
travel over the websocket, and it should not be typed into a browser tab that
anything on the machine can reach.

`configure` and `ask` know nothing about any particular provider and never
write the project's config: `.localcode/opencode.json` is yours to edit. What
localcode does there is run opencode's login flow against the credential store,
and put a question to whatever that config selected.

`configure-llamacpp` is the exception, and only because the alternative is
worse: a llama-server's models are whatever it was started with, and it will
say so if asked. Everything else in the file is left as it was found.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from ..docker.agent import AgentContainer, ensure_image
from ..driver.process import EventStream
from ..project import Project
from . import connect, llamacpp
from .container import environment

#: Read by `infra/docker/agent/ts/scripts/ask.ts`, the other half of `ask`.
SYSTEM_ENV = "LOCALCODE_ASK_SYSTEM"

#: How a container reports into a session, read by `src/session/client.ts`.
#: Absent for a terminal `ask`, which is what makes it write to stdout instead.
SESSION_ENV = "LOCALCODE_SESSION"
SESSION_URL_ENV = "LOCALCODE_SESSION_URL"
SECRET_ENV = "LOCALCODE_SECRET"
AGENT_ENV = "LOCALCODE_AGENT"

#: The runtime localcode's own scripts need, and where they live in the agent
#: image. Agreed with `infra/docker/agent/Dockerfile`, and the one place the
#: host says anything about them: a second script is one more constant here.
SCRIPT_RUNTIME = "bun"
SCRIPTS = "/opt/localcode/scripts"
ASK_SCRIPT = f"{SCRIPTS}/ask.ts"


async def configure(project: Project, provider: str | None = None) -> int:
    """Run opencode's own login flow against this project's credential store.

    localcode does not implement logging in. `opencode providers login` runs in
    the agents' image with your terminal attached, so API keys, OAuth, device
    codes and whatever a provider invents next all work exactly as they do in
    opencode, and go on working when opencode changes them.
    """
    return await connect.login(project, provider)


async def configure_llamacpp(
    project: Project,
    url: str = llamacpp.DEFAULT_URL,
    *,
    choose: Callable[[list[str]], str] | None = None,
) -> Path:
    """Write the project's config from a running llama-server.

    The server answers for everything except which of its models the project
    should use, since it serves all of them equally. `choose` is handed the ids
    it listed and returns the one to write as the top-level `model`; the CLI
    passes a chooser that asks at the terminal. Without one -- or from a server
    serving nothing -- the provider block is written with nothing selected,
    which is the file as this command used to leave it.
    """
    config = await llamacpp.config(url)
    listed = llamacpp.models(config)
    if listed and choose is not None:
        config = llamacpp.select(config, choose(listed))
    return llamacpp.write(project, config)


async def ask(project: Project, prompt: str, *, system: str = "") -> int:
    """One question to the configured model: the shortest end-to-end check.

    There is no model client on this side of docker, and no model chosen on it
    either. A throwaway container from the agents' image gets the project's
    config and credentials in its environment and the prompt on its stdin, and
    reads which model to use out of that config itself -- so the selection is
    made in exactly one place, the file, and the answer streams back as the
    model produces it. The prompt goes in on stdin rather than the environment
    so that it stays out of `docker inspect`.

    In there it resolves the provider by the same rules opencode does, against
    the same catalogue, so an `ask` that works is evidence that an agent run
    will -- which is the only reason this command is worth having.
    """
    await ensure_image()
    return await AgentContainer(
        project=project,
        name=project.ask_container("terminal"),
        role="ask",
        env={**environment(project), SYSTEM_ENV: system},
        entrypoint=SCRIPT_RUNTIME,
        command=[ASK_SCRIPT],
    ).feed(prompt)


def session_environment(project: Project, session_id: str, agent: str) -> dict[str, str]:
    """What a container needs to report into a session.

    The runtime secret, because a container authenticates on the same socket
    the browser does and there is no second protocol. It reaches the controller
    over the host gateway, which every container from this image already gets
    (`docker/agent.py:HOST_GATEWAY`) -- the ask container has no project
    network, so this is the only route it has.
    """
    return {
        SESSION_ENV: session_id,
        SESSION_URL_ENV: f"ws://host.docker.internal:{project.runtime.ws_port}/ws",
        SECRET_ENV: project.runtime.secret,
        AGENT_ENV: agent,
    }


async def ask_into(
    project: Project,
    session_id: str,
    prompt: str,
    *,
    events: EventStream,
    system: str = "",
    agent: str = "",
) -> int:
    """`ask`, with the answer going to a session instead of the terminal.

    The container's own stdout and stderr still go to `events` -- the process
    log and the command shelf -- rather than into the session. The two are
    different records: the session is what the agent chose to say, and this is
    what its container emitted, including whatever it managed to say if it died
    before the socket ever opened.
    """
    await ensure_image(events)
    return await AgentContainer(
        project=project,
        name=project.ask_container(session_id),
        role="ask",
        labels={"localcode.session": session_id},
        env={
            **environment(project),
            SYSTEM_ENV: system,
            **session_environment(project, session_id, agent),
        },
        entrypoint=SCRIPT_RUNTIME,
        command=[ASK_SCRIPT],
    ).stream(events, stdin=prompt)
