"""Built-in workflow definitions trusted by the controller.

The host owns discovery and validation; the executable loop lives in the
agent image at the path recorded here. Keeping this list explicit means a
websocket message can select a known process, never an arbitrary executable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any


class UnknownProcess(ValueError):
    """Raised when a caller names a process localcode does not ship."""


@dataclass(frozen=True)
class ProcessDefinition:
    """One built-in process and the capabilities its launcher exposes."""

    name: str
    title: str
    description: str
    script: str
    requires_persona: bool = True
    requires_story: bool = False
    interactive: bool = False
    roles: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9_-]*", self.name):
            raise ValueError(f"invalid process name {self.name!r}")
        if not self.title.strip() or not self.description.strip():
            raise ValueError("process title and description are required")
        path = PurePosixPath(self.script)
        if (
            not path.is_absolute()
            or path.parent != PurePosixPath("/opt/localcode/processes")
            or path.suffix != ".ts"
        ):
            raise ValueError(
                "process script must be a TypeScript file directly under "
                "/opt/localcode/processes"
            )

    def wire(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "requiresPersona": self.requires_persona,
            "requiresStory": self.requires_story,
            "interactive": self.interactive,
        }


CHAT = ProcessDefinition(
    name="chat",
    title="Chat",
    description="A continuing conversation with one persona.",
    script="/opt/localcode/processes/chat.ts",
    interactive=True,
)

STORY = ProcessDefinition(
    name="story",
    title="Implement story",
    description=(
        "Review one story against the codebase, implement it, and open a pull request."
    ),
    script="/opt/localcode/processes/story.ts",
    requires_story=True,
    roles=("story-pre-dev", "story-developer"),
)

BUILT_INS = (CHAT, STORY)


def _index(
    definitions: tuple[ProcessDefinition, ...],
) -> dict[str, ProcessDefinition]:
    indexed = {definition.name: definition for definition in definitions}
    if len(indexed) != len(definitions):
        raise ValueError("built-in process names must be unique")
    return indexed


_BY_NAME = _index(BUILT_INS)


def get(name: object) -> ProcessDefinition:
    """Resolve one trusted process name."""
    if not isinstance(name, str) or not name.strip():
        raise UnknownProcess("process is required")
    try:
        return _BY_NAME[name]
    except KeyError as exc:
        raise UnknownProcess(
            f"no process {name!r} -- have {[item.name for item in BUILT_INS]}"
        ) from exc


def catalog() -> list[dict[str, Any]]:
    """Definitions in their stable display order, ready for the websocket."""
    return [definition.wire() for definition in BUILT_INS]
