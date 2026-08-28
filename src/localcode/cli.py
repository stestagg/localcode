"""Entry point for the `localcode` command.

Placeholder shim: enough to prove the install works, nothing more. The real
subcommands (`init`, `run`) live in the vision doc, not here yet.
"""

from __future__ import annotations

import argparse
import sys

from . import __version__


def _hello(args: argparse.Namespace) -> int:
    print(f"hello, {args.name}! this is localcode {__version__}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="localcode",
        description="A bunch of agents all working together to build some software.",
    )
    parser.add_argument("--version", action="version", version=f"localcode {__version__}")

    # required=True so a bare `localcode` prints usage instead of falling through.
    sub = parser.add_subparsers(dest="command", required=True)

    hello = sub.add_parser("hello", help="say hello (a shim, to check the install)")
    hello.add_argument("name", nargs="?", default="world")
    hello.set_defaults(func=_hello)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
