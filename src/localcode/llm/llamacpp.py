"""Writing `.localcode/opencode.json` from a running llama-server.

llama-server knows what it is serving and opencode does not, so the provider
block that names its models is a thing to ask for rather than to type: it lists
them at `/v1/models` and reports the context it was started with at `/props`.

This writes the file. It does not merge with one already there.
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx

from ..project import Project

DEFAULT_URL = "http://localhost:8080"

#: The provider id this writes under, and the first half of every model id
#: opencode will resolve out of it: `llama.cpp/<what the server called it>`.
PROVIDER = "llama.cpp"

#: Half the window, up to a ceiling: room to answer, without claiming a model
#: will emit a hundred thousand tokens because it could read that many.
MAX_OUTPUT = 65536


class LlamaError(Exception):
    """llama-server could not be reached, or answered with something else."""


def base_url(url: str) -> str:
    """The compatible endpoint, from whatever was typed at the command line.

    A url ending in `/v1` is the same server: it is what every other tool asks
    for, so it is what people have to hand.
    """
    parts = urlsplit(url if "//" in url else f"//{url}", scheme="http")
    if not parts.hostname:
        raise LlamaError(f"{url!r} is not a url")
    path = parts.path.rstrip("/").removesuffix("/v1")
    return urlunsplit((parts.scheme, parts.netloc, f"{path}/v1", "", ""))


async def config(url: str, transport: httpx.AsyncBaseTransport | None = None) -> dict:
    """The opencode config for this server, models and all.

    A `llama serve` router serves several models and reports no one context
    length, so limits are left out and opencode's defaults apply.
    """
    endpoint = base_url(url)
    async with httpx.AsyncClient(timeout=30, transport=transport) as client:
        try:
            listed = (await client.get(f"{endpoint}/models")).json()["data"]
            props = (await client.get(f"{endpoint[:-3]}/props")).json()
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            raise LlamaError(f"{endpoint}: {exc}") from exc

    context = props.get("default_generation_settings", {}).get("n_ctx") or 0
    limit = (
        {"limit": {"context": context, "output": min(context // 2, MAX_OUTPUT)}}
        if context
        else {}
    )
    return {
        "$schema": "https://opencode.ai/config.json",
        "provider": {
            PROVIDER: {
                "npm": "@ai-sdk/openai-compatible",
                "name": "llama-server (local)",
                # An empty key on purpose: this server wants none, and leaving
                # the field out means the opposite -- go and find one.
                "options": {"baseURL": endpoint, "apiKey": ""},
                "models": {
                    model["id"]: {"name": f"{model['id']} (local)", **limit}
                    for model in listed
                },
            }
        },
    }


def models(config: dict) -> list[str]:
    """The ids this server listed, in the order it listed them."""
    return list(config["provider"][PROVIDER]["models"])


def select(config: dict, model: str) -> dict:
    """The same config, with one of its models named as the project default.

    Which model is the one thing a server cannot be asked: it serves all of
    them. The id is qualified with the provider it was found under, because
    that is what opencode resolves -- an unqualified name means a search of
    every provider, and this one is not in the catalogue to be found in.

    The key goes in ahead of the block it points at: the file is read by people
    as well as by opencode, and the selection is the part they came for.
    """
    selected = dict(config)
    provider = selected.pop("provider")
    selected["model"] = f"{PROVIDER}/{model}"
    selected["provider"] = provider
    return selected


def write(project: Project, config: dict) -> Path:
    """Put it in the project's config file, replacing whatever was there."""
    path = project.localcode_dir / "opencode.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, indent=2) + "\n")
    return path
