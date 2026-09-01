"""The hub container: caddy in front, gitea behind it, the web ui on top.

One per project, long-lived for as long as `localcode run` is in the foreground.
It mounts the repo read-write so gitea's data lands in `.localcode/state/`, and
it reaches back to the controller on the host for the event stream.
"""

from __future__ import annotations

import os

from ..driver.process import EventStream
from ..project import Project
from ..source import build_context, dockerfile, web_dir
from . import client

IMAGE = "localcode-hub"
DEV_IMAGE = "localcode-hub-dev"
ALIAS = "localcode"


class Hub:
    """The hub for one project, in either its static or dev-ui form."""

    def __init__(self, project: Project, *, dev: bool = False) -> None:
        self.project = project
        self.dev = dev

    @property
    def image(self) -> str:
        return DEV_IMAGE if self.dev else IMAGE

    async def ensure_image(self, events: EventStream, *, rebuild: bool = False) -> None:
        if not rebuild and await client.image_exists(self.image):
            return
        code = await client.build(
            self.image,
            dockerfile=dockerfile("hub"),
            context=str(build_context()),
            target="hub-dev" if self.dev else "hub",
            events=events,
        )
        if code != 0:
            raise client.DockerError(f"building {self.image} failed ({code})")

    async def start(self, ws_port: int) -> None:
        project = self.project
        await client.remove(project.hub_container)
        await client.network_ensure(project.network, project.labels)

        volumes = [(str(project.path), "/repo")]
        if self.dev:
            # The ui source, live; node_modules in a volume on top of it so the
            # install survives restarts and stays out of the host checkout.
            volumes += [
                (str(web_dir()), "/web"),
                (f"localcode-ui-{project.id}", "/web/node_modules"),
            ]

        await client.check(
            *client.run_args(
                self.image,
                name=project.hub_container,
                labels={**project.labels, "localcode.role": "hub"},
                env={
                    "LOCALCODE_UID": str(os.getuid()),
                    "LOCALCODE_GID": str(os.getgid()),
                    "LOCALCODE_SECRET": project.runtime.secret,
                    "LOCALCODE_WS_UPSTREAM": f"host.docker.internal:{ws_port}",
                    "LOCALCODE_UI_MODE": "dev" if self.dev else "static",
                    "LOCALCODE_PORT": str(project.runtime.http_port),
                },
                volumes=volumes,
                ports=[(f"127.0.0.1:{project.runtime.http_port}", "80")],
                network=project.network,
                # So the caddy inside can reach the controller out here. Docker
                # Desktop resolves this already; on linux it needs saying.
                add_hosts=["host.docker.internal:host-gateway"],
                alias=ALIAS,
                detach=True,
            )
        )

    async def stop(self) -> None:
        await client.stop(self.project.hub_container)
        await client.remove(self.project.hub_container)
