"""Throwaway containers from the agent image.

One per piece of work: a fresh clone from gitea, whatever the runner does to it,
a branch pushed and a pull request opened. Normally nothing is mounted from the
host -- the container has no way to touch the checkout, only the master repo
through gitea -- and it is gone the moment what it was started for exits.

Everything localcode starts from this image goes through `AgentContainer`,
including the two things that are not agent runs: opencode's login flow, and
one question to a model. They differ only in what is mounted, what runs, and
where the output goes -- so the one place that builds a `docker run` is here,
rather than each caller assembling its own and getting a flag wrong.
"""

from __future__ import annotations

import asyncio
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from uuid import uuid4

from ..driver.process import Event, EventStream
from ..llm import container as llm_container
from ..project import Project
from ..source import build_context, dockerfile
from . import client

IMAGE = "localcode-agent"

#: How a container reaches a server on the machine that started it. Docker
#: Desktop resolves this already; on linux it needs saying.
HOST_GATEWAY = "host.docker.internal:host-gateway"


@dataclass(frozen=True)
class AgentContainer:
    """One container from the agent image, thrown away when it exits.

    Always `--rm`. Nothing localcode starts from this image is meant to
    outlive the thing it was started for, so there is no option not to.
    """

    project: Project
    name: str
    role: str
    env: Mapping[str, str] = field(default_factory=dict)
    volumes: Sequence[tuple[str, str]] = ()
    entrypoint: str | None = None
    command: Sequence[str] = ()
    network: str | None = None
    #: Anything worth being able to pick this container out by later. The
    #: project and the role are added for you.
    labels: Mapping[str, str] = field(default_factory=dict)

    def argv(self, *, stdin: bool = False, tty: bool = False) -> list[str]:
        return client.run_args(
            IMAGE,
            name=self.name,
            labels={
                **self.project.labels,
                "localcode.role": self.role,
                **self.labels,
            },
            env=self.env,
            volumes=self.volumes,
            network=self.network,
            add_hosts=[HOST_GATEWAY],
            stdin=stdin,
            tty=tty,
            entrypoint=self.entrypoint,
            command=self.command,
        )

    async def stream(self, events: EventStream, *, stdin: str | None = None) -> int:
        """Run it, with everything it says going to `events`.

        `stdin` is for input the container needs and nobody should be able to
        read off a `docker inspect` -- a prompt, chiefly. `feed` does the same
        for the terminal; this is the streamed half of the pair.
        """
        return await client.stream(self.argv(stdin=stdin is not None), events, stdin=stdin)

    async def attach(self) -> int:
        """Run it with the terminal handed over, for something interactive.

        Straight through rather than streamed: an interactive prompt needs the
        cursor control that streaming would eat. Off the event loop, since it
        sits there for as long as the person takes.
        """
        return await self._call(self.argv(stdin=True, tty=True), None)

    async def feed(self, text: str) -> int:
        """Run it with `text` on its stdin, its output going to the terminal."""
        return await self._call(self.argv(stdin=True), text)

    @staticmethod
    async def _call(argv: list[str], text: str | None) -> int:
        def run() -> int:
            process = subprocess.Popen(
                ["docker", *argv],
                stdin=subprocess.PIPE if text is not None else None,
                text=True,
            )
            try:
                process.communicate(text)
            except KeyboardInterrupt:
                process.kill()
                raise
            return process.returncode

        return await asyncio.to_thread(run)


async def to_terminal(event: Event) -> None:
    """Everything a container says, on the terminal that asked for it.

    The default, because a build is the one thing that happens here slowly
    enough to be worth watching and it only happens the first time. A caller
    with somewhere better to put the output -- the web ui -- passes its own.
    """
    if event["type"] in ("stdout", "stderr"):
        print(event["data"], end="", flush=True)


async def ensure_image(
    events: EventStream = to_terminal, *, rebuild: bool = False
) -> None:
    if not rebuild and await client.image_exists(IMAGE):
        return
    code = await client.build(
        IMAGE,
        dockerfile=dockerfile("agent"),
        context=str(build_context()),
        events=events,
    )
    if code != 0:
        raise client.DockerError(f"building {IMAGE} failed ({code})")


class UnknownAgent(Exception):
    pass


class UnknownPersona(UnknownAgent):
    pass


class UnknownRole(UnknownAgent):
    pass


async def run(
    project: Project,
    persona_name: str,
    role_name: str,
    runner: str,
    events: EventStream,
) -> int:
    """Run one persona-role composition to completion, streaming its output."""
    persona = project.persona(persona_name)
    if persona is None:
        available = [item.name for item in project.personas()]
        raise UnknownPersona(f"no persona {persona_name!r} -- have {available}")

    role = project.role(role_name)
    if role is None:
        available = [item.name for item in project.roles()]
        raise UnknownRole(f"no role {role_name!r} -- have {available}")

    token = project.runtime.personas.get(persona_name)
    if token is None:
        raise UnknownPersona(f"persona {persona_name!r} has not been provisioned")

    await ensure_image(events)

    run_id = uuid4().hex[:8]
    container = AgentContainer(
        project=project,
        name=project.agent_container(run_id),
        role="agent",
        network=project.network,
        labels={
            "localcode.persona": persona_name,
            "localcode.role": role_name,
            "localcode.runner": runner,
        },
        env={
            "LOCALCODE_GITEA_URL": project.internal_gitea_url,
            "LOCALCODE_USER": persona_name,
            "LOCALCODE_AGENT": persona_name,
            "LOCALCODE_PERSONA": persona_name,
            "LOCALCODE_ROLE": role_name,
            "LOCALCODE_PERSONA_PROMPT": persona.prompt,
            "LOCALCODE_ROLE_PROMPT": role.prompt,
            "LOCALCODE_TOKEN": token,
            "LOCALCODE_OWNER": project.gitea_owner,
            "LOCALCODE_REPO": project.gitea_repo,
            "LOCALCODE_BASE": "main",
            "LOCALCODE_METADATA_BRANCH": "localcode",
            "LOCALCODE_BRANCH": (
                f"agent/{persona_name}/{role_name}-{runner}-{run_id}"
            ),
            "LOCALCODE_RUNNER": runner,
            "LOCALCODE_RUN_ID": run_id,
            # The project's opencode config and credentials, which the
            # entrypoint writes to the paths the binary reads. Absent entirely
            # when no provider has been configured.
            **llm_container.environment(project),
        },
    )
    return await container.stream(events)


async def stop_all(project: Project) -> None:
    """Kill every agent container this project has out, for shutdown."""
    for name in await client.by_label("localcode.project", project.id):
        if name != project.hub_container:
            await client.remove(name)
