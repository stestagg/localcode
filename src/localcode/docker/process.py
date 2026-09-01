"""Run a built-in process in an isolated clone of the project."""

from __future__ import annotations

import json

from ..driver.process import EventStream
from ..driver.processes import ProcessDefinition
from ..llm import container as llm_container
from ..llm.console import session_environment
from ..project import Project
from .agent import AgentContainer, UnknownPersona, UnknownRole, ensure_image


async def run(
    project: Project,
    definition: ProcessDefinition,
    persona_name: str,
    session_id: str,
    events: EventStream,
    *,
    story: str = "",
) -> int:
    """Run one trusted process script in a fresh repository clone."""
    persona = project.persona(persona_name)
    if persona is None:
        raise UnknownPersona(
            f"no persona {persona_name!r} -- have "
            f"{[item.name for item in project.personas()]}"
        )

    token = project.runtime.personas.get(persona_name)
    if token is None:
        raise UnknownPersona(f"persona {persona_name!r} has not been provisioned")

    role_prompts: dict[str, str] = {}
    for role_name in definition.roles:
        role = project.role(role_name)
        if role is None:
            raise UnknownRole(
                f"process {definition.name!r} requires role {role_name!r}"
            )
        role_prompts[role_name] = role.prompt

    await ensure_image(events)
    container = AgentContainer(
        project=project,
        name=project.process_container(session_id),
        role="process",
        network=project.network,
        labels={
            "localcode.process": definition.name,
            "localcode.persona": persona_name,
            "localcode.session": session_id,
        },
        env={
            "LOCALCODE_GITEA_URL": project.internal_gitea_url,
            "LOCALCODE_USER": persona_name,
            "LOCALCODE_TOKEN": token,
            "LOCALCODE_OWNER": project.gitea_owner,
            "LOCALCODE_REPO": project.gitea_repo,
            "LOCALCODE_METADATA_BRANCH": "localcode",
            "LOCALCODE_BASE": "main",
            "LOCALCODE_BRANCH": (
                f"process/{persona_name}/{definition.name}-{session_id}"
            ),
            "LOCALCODE_PROCESS": definition.name,
            "LOCALCODE_PERSONA": persona_name,
            "LOCALCODE_PERSONA_PROMPT": persona.prompt,
            "LOCALCODE_ROLE_PROMPTS": json.dumps(role_prompts),
            **({"LOCALCODE_STORY_PATH": story} if story else {}),
            **llm_container.environment(project),
            **session_environment(project, session_id, persona_name),
        },
        entrypoint="/process-entrypoint.sh",
        command=[definition.script],
    )
    return await container.stream(events)
