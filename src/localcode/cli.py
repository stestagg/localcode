"""Entry point for the `localcode` command."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import click

from . import __version__, repo
from .llm import console as llm
from .llm.config import ConfigError
from .project import DEFAULT_HTTP_PORT, NotAProject, Project
from .scaffold import scaffold


class Failed(click.ClickException):
    """A failure reported in localcode's voice rather than click's `Error:`."""

    def show(self, file: object = None) -> None:
        for line in str(self.message).splitlines():
            click.echo(f"localcode: {line}", err=True)


class Localcode(click.Group):
    """The top level, with localcode's own failures turned into clean exits.

    Wrapping dispatch here rather than every command means a command body can
    let a bad project or an unreadable config raise, and still not put a
    traceback in front of someone who simply ran it in the wrong directory.
    """

    def invoke(self, ctx: click.Context) -> object:
        try:
            return super().invoke(ctx)
        except NotAProject as exc:
            raise Failed(f"{exc}\nrun `localcode init` there first") from exc
        except ConfigError as exc:
            raise Failed(str(exc)) from exc


def _done(code: int) -> None:
    """Hand a command's exit code back to click, which owns the process exit."""
    if code:
        raise SystemExit(code)


def _project(path: str) -> Project:
    """The project at `path`, moving to the metadata branch if that is where it is.

    `.localcode/` deliberately does not exist on `main`, so a checkout sitting
    on main looks unconfigured until the branch it lives on is checked out.
    """
    try:
        return Project.find(path)
    except NotAProject:
        root = repo.root(Path(path).resolve())
        if root is None or not repo.branch_exists(root, repo.LOCALCODE_BRANCH):
            raise
        repo.prepare_localcode_branch(root)
        return Project.find(root)


def _here() -> Project:
    """The project the `llm` commands act on: the one this directory is in.

    Which directory that is has already been settled by `-C`, so the `llm`
    commands take no path of their own: a positional that sometimes means a
    path and sometimes means a provider or a prompt would be a guessing game.
    """
    return _project(".")


@click.group(cls=Localcode, context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(
    __version__, "--version", prog_name="localcode", message="%(prog)s %(version)s"
)
@click.option(
    "-C",
    "chdir",
    metavar="PATH",
    type=click.Path(exists=True, file_okay=False),
    help="run as if localcode had been started in PATH",
)
def cli(chdir: str | None) -> None:
    """A bunch of agents all working together to build some software."""
    # Like `git -C`: every command then works on that project, including the
    # ones whose only notion of which project they mean is the directory they
    # were started in.
    if chdir:
        os.chdir(chdir)


@cli.command()
@click.argument("path", default=".")
def init(path: str) -> None:
    """Set a repo up for localcode."""
    target = Path(path).resolve()
    target.mkdir(parents=True, exist_ok=True)
    project = scaffold(target)
    click.echo(f"localcode: set up {project.name} in {project.path}")


@cli.command()
@click.argument("url")
@click.argument("path", required=False)
def clone(url: str, path: str | None) -> None:
    """Clone a repo and set it up for localcode."""
    # `git clone <url>` with no destination names the directory after the repo;
    # do the same rather than make the argument mandatory.
    target = Path(path or url.rstrip("/").split("/")[-1].removesuffix(".git")).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    repo.clone(url, target)
    project = scaffold(target)
    click.echo(f"localcode: cloned {url} into {project.path}")


@cli.command()
@click.argument("path", default=".")
@click.option(
    "--port", type=int, help=f"published HTTP port (default: {DEFAULT_HTTP_PORT})"
)
@click.option(
    "--dev-ui",
    is_flag=True,
    help="serve the web ui from a live vite server instead of the built one",
)
@click.option("--rebuild", is_flag=True, help="rebuild the images first")
@click.option("--browser/--no-browser", default=True, help="open a browser at start-up")
def run(
    path: str, port: int | None, dev_ui: bool, rebuild: bool, browser: bool
) -> None:
    """Bring a project up and serve it."""
    from .control import run as control_run

    project = _project(path)
    _done(
        asyncio.run(
            control_run(
                project, dev=dev_ui, rebuild=rebuild, browser=browser, port=port
            )
        )
    )


@cli.group()
def llm_group() -> None:
    """Logging in to a provider, and putting a question to a model."""


# Named for the command rather than the function, since `llm` is the module.
cli.add_command(llm_group, "llm")


@llm_group.command("configure")
@click.argument("provider", required=False)
def configure(provider: str | None) -> None:
    """Log in to a provider, using opencode's own flow in a container."""
    _done(asyncio.run(llm.configure(_here(), provider)))


def _pick_model(models: list[str]) -> str:
    """Which of a llama-server's models the project uses by default.

    Picked by number rather than typed back: the ids are file names as often as
    they are model names, and long enough that retyping one is a way to get it
    wrong. A server with one model is shown but not asked about -- there is no
    choice to make, and a prompt with a single answer is only in the way.
    """
    click.echo("localcode: this server serves")
    for index, model in enumerate(models, 1):
        click.echo(f"  {index}. {model}")
    if len(models) == 1:
        return models[0]
    chosen = click.prompt(
        "localcode: which one is the default",
        type=click.IntRange(1, len(models)),
        default=1,
    )
    return models[chosen - 1]


@llm_group.command("configure-llamacpp")
@click.argument("url", default=llm.llamacpp.DEFAULT_URL)
def configure_llamacpp(url: str) -> None:
    """Write the config for the models a llama-server serves."""
    try:
        path = asyncio.run(llm.configure_llamacpp(_here(), url, choose=_pick_model))
    except llm.llamacpp.LlamaError as exc:
        raise Failed(f"llama-server: {exc}") from exc
    click.echo(f"localcode: wrote {path}")


@llm_group.command("ask")
@click.argument("prompt", nargs=-1, required=True)
@click.option("--system", default="", help="a system prompt")
def ask(prompt: tuple[str, ...], system: str) -> None:
    """Put one question to the configured model, in a container of its own."""
    _done(asyncio.run(llm.ask(_here(), " ".join(prompt), system=system)))


def main(argv: list[str] | None = None) -> int:
    """Run the CLI. Click owns the exit code, so this only returns on success."""
    cli.main(args=argv, prog_name="localcode")
    return 0


if __name__ == "__main__":
    sys.exit(main())
