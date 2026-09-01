"""Where localcode's own source lives, for the image builds that need it."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class NoSource(Exception):
    """Raised when localcode is installed without the docker build context."""


def build_context() -> Path:
    """The repo root, which is the docker build context for every image."""
    if not (ROOT / "infra" / "docker").is_dir():
        raise NoSource(
            f"no infra/docker under {ROOT}: run localcode from a checkout, "
            "since the images are built from it"
        )
    return ROOT


def dockerfile(image: str) -> str:
    return str(build_context() / "infra" / "docker" / image / "Dockerfile")


def web_dir() -> Path:
    return build_context() / "web"
