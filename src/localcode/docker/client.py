"""An async wrapper over the `docker` CLI.

The CLI rather than a client library: everything localcode needs is a handful of
verbs, and shelling out means container output arrives as the same
`start`/`stdout`/`stderr`/`exit` events the driver already speaks, so a run can
be streamed to a browser without a translation layer.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from ..driver.process import Event, EventStream, run_command


class DockerError(Exception):
    pass


async def _quiet(argv: Sequence[str]) -> tuple[int, str, str]:
    """Run a command to completion, collecting its output rather than streaming."""
    out: list[str] = []
    err: list[str] = []

    async def collect(event: Event) -> None:
        if event["type"] == "stdout":
            out.append(event["data"])
        elif event["type"] in ("stderr", "error"):
            err.append(event.get("data") or event.get("message", ""))

    code = await run_command(argv, collect)
    return code, "".join(out), "".join(err)


async def capture(*args: str) -> tuple[int, str]:
    """Run `docker <args>`, returning its exit code and stdout."""
    code, out, _ = await _quiet(["docker", *args])
    return code, out.strip()


async def check(*args: str) -> str:
    """Run `docker <args>`, raising if it fails."""
    code, out, err = await _quiet(["docker", *args])
    if code != 0:
        raise DockerError(f"docker {' '.join(args)} failed ({code}): {err.strip()}")
    return out.strip()


async def stream(
    argv: Sequence[str], events: EventStream, *, stdin: str | None = None
) -> int:
    """Run `docker <argv>` with its output streamed to `events`."""
    return await run_command(["docker", *argv], events, stdin=stdin)


async def available() -> bool:
    code, _ = await capture("version", "--format", "{{.Server.Version}}")
    return code == 0


# --- images ------------------------------------------------------------------


async def image_exists(tag: str) -> bool:
    code, _ = await capture("image", "inspect", tag)
    return code == 0


async def build(
    tag: str,
    *,
    dockerfile: str,
    context: str,
    target: str | None = None,
    events: EventStream,
) -> int:
    argv = ["build", "-t", tag, "-f", dockerfile]
    if target:
        argv += ["--target", target]
    argv.append(context)
    return await stream(argv, events)


# --- networks ----------------------------------------------------------------


async def network_ensure(name: str, labels: Mapping[str, str]) -> None:
    code, _ = await capture("network", "inspect", name)
    if code == 0:
        return
    argv = ["network", "create"]
    for key, value in labels.items():
        argv += ["--label", f"{key}={value}"]
    argv.append(name)
    await check(*argv)


async def network_remove(name: str) -> None:
    await capture("network", "rm", name)


# --- containers --------------------------------------------------------------


def run_args(
    image: str,
    *,
    name: str,
    labels: Mapping[str, str] = {},
    env: Mapping[str, str] = {},
    volumes: Sequence[tuple[str, str]] = (),
    ports: Sequence[tuple[str, str]] = (),
    network: str | None = None,
    alias: str | None = None,
    add_hosts: Sequence[str] = (),
    detach: bool = False,
    stdin: bool = False,
    tty: bool = False,
    entrypoint: str | None = None,
    command: Sequence[str] = (),
) -> list[str]:
    """The argument list for one `docker run`."""
    argv = ["run", "--rm", "--name", name]
    argv += ["--detach"] if detach else []
    # Without this docker gives the container a closed stdin, and anything
    # waiting to be fed on it reads nothing at all.
    argv += ["--interactive"] if stdin else []
    argv += ["--tty"] if tty else []
    for key, value in labels.items():
        argv += ["--label", f"{key}={value}"]
    for key, value in env.items():
        argv += ["-e", f"{key}={value}"]
    for source, target in volumes:
        argv += ["-v", f"{source}:{target}"]
    for published, container in ports:
        argv += ["-p", f"{published}:{container}"]
    if network:
        argv += ["--network", network]
    if alias:
        argv += ["--network-alias", alias]
    for host in add_hosts:
        argv += ["--add-host", host]
    if entrypoint:
        argv += ["--entrypoint", entrypoint]
    return [*argv, image, *command]


async def running(name: str) -> bool:
    code, out = await capture("inspect", "-f", "{{.State.Running}}", name)
    return code == 0 and out == "true"


async def stop(name: str) -> None:
    await capture("stop", "--time", "10", name)


async def remove(name: str) -> None:
    await capture("rm", "-f", name)


async def by_label(label: str, value: str) -> list[str]:
    """Names of all containers, running or not, carrying `label=value`."""
    _, out = await capture(
        "ps", "-a", "--filter", f"label={label}={value}", "--format", "{{.Names}}"
    )
    return out.split("\n") if out else []
