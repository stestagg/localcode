"""The actions a client can trigger. Importing this module registers them."""

from __future__ import annotations

import asyncio

from .. import repo as git
from ..docker import agent, client, process as process_container
from ..driver import processes
from ..gitea import setup
from ..gitea.api import Gitea
from ..llm import console as llm
from ..project import STORY_STAGES
from . import sessions
from .ws import WsCommand, send_message, ws_handler


@ws_handler
async def status(command: WsCommand) -> None:
    """Everything the ui needs to draw itself."""
    project = command.project
    personas, persona_issues = project.load_personas()
    roles, role_issues = project.load_roles()
    await command.send(
        {
            "type": "status",
            "project": {
                "name": project.name,
                "path": str(project.path),
                "id": project.id,
                "head": git.head_summary(project.path),
                "branches": git.branches(project.path),
                "readme": git.file_at_ref(
                    project.path, git.MAIN_BRANCH, "README.md"
                ),
                "readmeUrl": (
                    f"{project.gitea_repo_url}/src/branch/"
                    f"{git.MAIN_BRANCH}/README.md"
                ),
            },
            "gitea": {
                "url": project.gitea_repo_url,
                "repo": f"{project.gitea_owner}/{project.gitea_repo}",
                "user": setup.HUMAN_USER,
                "password": project.runtime.human_password,
            },
            "hub": {
                "container": project.hub_container,
                "running": await client.running(project.hub_container),
            },
            "personas": [
                {
                    "name": persona.name,
                    "prompt": persona.prompt,
                    "promptPreview": persona.preview(),
                    "fileUrl": project.persona_file_url(persona),
                    "editUrl": project.persona_edit_url(persona),
                }
                for persona in personas
            ],
            "personaIssues": [
                {
                    "name": issue.name,
                    "message": issue.message,
                    "fileUrl": project.persona_path_url(issue.path),
                    "editUrl": project.persona_path_edit_url(issue.path),
                }
                for issue in persona_issues
            ],
            "roles": [
                {
                    "name": role.name,
                    "prompt": role.prompt,
                    "promptPreview": role.preview(),
                    "fileUrl": project.role_file_url(role),
                    "editUrl": project.role_edit_url(role),
                }
                for role in roles
            ],
            "roleIssues": [
                {
                    "name": issue.name,
                    "message": issue.message,
                    "fileUrl": project.role_path_url(issue.path),
                    "editUrl": project.role_path_edit_url(issue.path),
                }
                for issue in role_issues
            ],
            "runners": ["hello", "opencode"],
            "processes": processes.catalog(),
        }
    )


@ws_handler(name="agent.run")
async def agent_run(command: WsCommand) -> None:
    """Spawn one throwaway agent container and stream it to every open tab."""
    project = command.project
    persona = command.data.get("persona")
    if not isinstance(persona, str) or not persona.strip():
        raise agent.UnknownPersona("persona is required")
    role = command.data.get("role")
    if not isinstance(role, str) or not role.strip():
        raise agent.UnknownRole("role is required")
    runner = command.data.get("runner", "hello")
    async with command.recorded(command.broadcast_stream()) as events:
        await agent.run(project, persona, role, runner, events)
    warning = ""
    try:
        await setup.sync_metadata(project)
    except Exception as exc:
        warning = f"{type(exc).__name__}: {exc}"
    await command.publish(
        {
            "type": "metadata.changed",
            **({"message": warning} if warning else {}),
        }
    )


@ws_handler(name="personas.sync")
async def personas_sync(command: WsCommand) -> None:
    """Give any newly-defined persona its gitea account, without a restart."""
    project = command.project
    async with Gitea(project.gitea_api, project.runtime.automation_token) as gitea:
        added = await setup.sync_personas(project, gitea)
    project.save_runtime()
    await command.publish({"type": "personas", "added": added})


@ws_handler(name="agent.stop")
async def agent_stop(command: WsCommand) -> None:
    """Kill anything this project still has running."""
    await agent.stop_all(command.project)
    await command.publish({"type": "agents", "stopped": True})


@ws_handler(name="gitea.pulls")
async def gitea_pulls(command: WsCommand) -> None:
    """The open pull requests agents have left for review."""
    project = command.project
    async with Gitea(project.gitea_api, project.runtime.automation_token) as gitea:
        pulls = await gitea.list_pulls(project.gitea_owner, project.gitea_repo)
    await command.send(
        {
            "type": "pulls",
            "pulls": [
                {
                    "number": pull["number"],
                    "title": pull["title"],
                    "branch": pull["head"]["ref"],
                    "url": pull["html_url"],
                }
                for pull in pulls
            ],
        }
    )


@ws_handler(name="stories.list")
async def stories_list(command: WsCommand) -> None:
    """Stories for requested lifecycle stages; archived stages stay lazy."""
    requested = command.data.get("states", [])
    if not isinstance(requested, list) or any(
        not isinstance(state, str) or state not in STORY_STAGES
        for state in requested
    ):
        raise ValueError("states must be a list of known story stages")

    project = command.project
    await command.send(
        {
            "type": "stories",
            "states": {
                state: [
                    {
                        "number": story.number,
                        "title": story.title,
                        "date": story.date,
                        "prId": story.pr_id,
                        "path": story.path.relative_to(project.path).as_posix(),
                        "fileUrl": project.story_file_url(story),
                    }
                    for story in project.stories(state)
                ]
                for state in dict.fromkeys(requested)
            },
        }
    )


@ws_handler
async def ask(command: WsCommand) -> None:
    """One question to the configured model, answered into a session.

    Both layers at once, and the clearest example of why they are two. The
    container's stdout goes to the command shelf and to `state/process_logs/`;
    the answer the model produced goes to the session, attributed to the agent
    that was asked and kept in `state/sessions/`. A container that fails before
    it reaches a provider leaves the first and not the second.

    The persona picked is the one whose instructions become the system prompt --
    that is what choosing a persona on the Ask tab means.

    This returns as soon as the run is under way. Waiting here would tie the
    answer to the socket that asked for it, and a tab that reloads mid-question
    would take the run's cleanup down with it while the container carried on.
    """
    project = command.project
    prompt = command.data.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("prompt must be a non-empty string")

    name = command.data.get("persona")
    if not isinstance(name, str) or not name.strip():
        raise agent.UnknownPersona("persona is required")
    persona = project.persona(name)
    if persona is None:
        raise agent.UnknownPersona(
            f"no persona {name!r} -- have {[item.name for item in project.personas()]}"
        )

    session = command.sessions.create(
        command.data.get("session"), title=f"ask {name}", agent=name, process="ask"
    )
    session.container = project.ask_container(session.id)
    # The caller is watching before anything is posted, so the question it just
    # asked is the first thing it sees rather than something it has to add.
    session.viewers.add(command.socket)
    await send_message(command.socket, session.history())

    question = session.post(sessions.USER, sessions.TEXT, text=prompt)
    await session.broadcast({"type": "session.message", "post": question.wire()})

    command.sessions.start(_answer(command, session, prompt, persona.prompt))


@ws_handler(name="process.start")
async def process_start(command: WsCommand) -> None:
    """Start one trusted built-in process and return while it keeps running."""
    definition = processes.get(command.data.get("process"))
    name = command.data.get("persona")
    if definition.requires_persona and (
        not isinstance(name, str) or not name.strip()
    ):
        raise agent.UnknownPersona("persona is required")
    if not isinstance(name, str):
        name = ""
    persona = command.project.persona(name)
    if definition.requires_persona and persona is None:
        raise agent.UnknownPersona(
            f"no persona {name!r} -- have "
            f"{[item.name for item in command.project.personas()]}"
        )

    story = ""
    if definition.requires_story:
        story = command.project.story_path(command.data.get("story"))

    session = command.sessions.create(
        command.data.get("session"),
        title=(
            f"{definition.title.lower()} "
            f"{story.rsplit('/', 1)[-1] if story else name}"
        ).strip(),
        agent=name,
        process=definition.name,
    )
    session.container = command.project.process_container(session.id)
    session.viewers.add(command.socket)
    await send_message(command.socket, session.history())
    command.sessions.start(
        _run_process(command, session, definition, name, story=story)
    )


async def _run_process(
    command: WsCommand,
    session: sessions.Session,
    definition: processes.ProcessDefinition,
    persona: str,
    *,
    story: str = "",
) -> None:
    """Watch one process container and settle its session from real outcome."""
    outcome = sessions.ABANDONED
    try:
        async with command.recorded(command.broadcast_stream()) as events:
            code = await process_container.run(
                command.project,
                definition,
                persona,
                session.id,
                events,
                story=story,
            )
        outcome = sessions.FINISHED if code == 0 else sessions.FAILED
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        outcome = sessions.FAILED
        note = session.post(
            "localcode", sessions.STATUS, text=f"{type(exc).__name__}: {exc}"
        )
        await session.broadcast({"type": "session.message", "post": note.wire()})
    finally:
        for message in session.flush():
            await session.broadcast(
                {"type": "session.message", "post": message.wire()}
            )
        if session.live:
            session.set_state(outcome)
            await session.broadcast(
                {"type": "session.closed", "state": session.state}
            )


async def _answer(
    command: WsCommand, session: sessions.Session, prompt: str, system: str
) -> None:
    """Run one ask to the end, and say honestly how it ended.

    The outcome starts as `ABANDONED` and is only moved off it by something
    that actually watched the run finish. Marking a session complete because
    this coroutine stopped executing would be a claim nobody here is in a
    position to make -- a cancellation and a good answer look identical from
    inside a `finally`.
    """
    outcome = sessions.ABANDONED
    try:
        async with command.recorded(command.broadcast_stream()) as events:
            code = await llm.ask_into(
                command.project,
                session.id,
                prompt,
                events=events,
                system=system,
                agent=session.agent,
            )
        outcome = sessions.FINISHED if code == 0 else sessions.FAILED
    except asyncio.CancelledError:
        # The controller is going down. It stays abandoned, and the container
        # is removed by the shutdown that cancelled this.
        raise
    except Exception as exc:
        # Nothing is waiting on this coroutine any more, so the only place a
        # failure can be reported is where the person is already looking.
        outcome = sessions.FAILED
        note = session.post(
            "localcode", sessions.STATUS, text=f"{type(exc).__name__}: {exc}"
        )
        await session.broadcast({"type": "session.message", "post": note.wire()})
    finally:
        # A partial answer is still an answer: a run that was stopped keeps
        # whatever the model had said by the time it was cut off.
        for message in session.flush():
            await session.broadcast(
                {"type": "session.message", "post": message.wire()}
            )
        # A stop is a person's decision, and an agent that closed on its way
        # out has already said how it went. Either way the session has settled
        # and been announced, so this only speaks for a run nothing else spoke
        # for -- which is also the only case where a second `session.closed`
        # would not be a repeat.
        if session.live:
            session.set_state(outcome)
            await session.broadcast(
                {"type": "session.closed", "state": session.state}
            )


# --- sessions ----------------------------------------------------------------
#
# Two audiences on one socket. A browser subscribes, watches and steers; an
# agent container attaches, posts and looks for instructions between steps.
# Both authenticate with the runtime secret, so nothing here distinguishes them
# beyond which actions they happen to call.


def _session(command: WsCommand) -> sessions.Session:
    return command.sessions.get(command.data.get("session"))


@ws_handler(name="session.list")
async def session_list(command: WsCommand) -> None:
    """Recent sessions, so a reloaded tab can find its way back into one."""
    limit = command.data.get("limit", 50)
    if not isinstance(limit, int) or not 0 < limit <= 200:
        raise ValueError("limit must be between 1 and 200")
    await command.send({"type": "sessions", "sessions": command.sessions.list(limit)})


@ws_handler(name="session.subscribe")
async def session_subscribe(command: WsCommand) -> None:
    """Watch one session, starting with everything already said in it."""
    session = _session(command)
    session.viewers.add(command.socket)
    await send_message(command.socket, session.history())


@ws_handler(name="session.unsubscribe")
async def session_unsubscribe(command: WsCommand) -> None:
    session = _session(command)
    session.viewers.discard(command.socket)


@ws_handler(name="session.control")
async def session_control(command: WsCommand) -> None:
    """Pause, resume or stop. Cooperative: the agent acts on it when it looks.

    A stop is the exception, because an agent that is wedged is exactly when
    one is pressed: the container behind the session is removed as well, so the
    button means something even when nothing is listening.
    """
    session = _session(command)
    control = command.data.get("control")
    state = {"pause": sessions.PAUSED, "resume": sessions.RUNNING, "stop": sessions.STOPPED}.get(
        control if isinstance(control, str) else ""
    )
    if state is None:
        raise ValueError("control must be one of pause, resume, stop")

    session.set_state(state)
    event = {"type": "session.state", "state": session.state}
    await session.broadcast(event)
    await session.signal(event)

    if state == sessions.STOPPED and session.container:
        await client.remove(session.container)


@ws_handler(name="session.input")
async def session_input(command: WsCommand) -> None:
    """Something a person typed: into the transcript, and into the queue."""
    text = command.data.get("text")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("text must be a non-empty string")

    session = _session(command)
    message = session.post(sessions.USER, sessions.TEXT, text=text)
    session.inputs.append(message)
    await session.broadcast({"type": "session.message", "post": message.wire()})
    await session.signal({"type": "session.input", "input": message.wire()})


@ws_handler(name="session.attach")
async def session_attach(command: WsCommand) -> None:
    """An agent joining its session. The state comes back straight away, so
    one that attached after a stop finds out rather than working on."""
    session = _session(command)
    session.workers.add(command.socket)
    await send_message(
        command.socket,
        {"type": "session.state", "session": session.id, "state": session.state},
    )


@ws_handler(name="session.post")
async def session_post(command: WsCommand) -> None:
    """One message from an agent, or one more piece of one.

    `done` is false while an answer is still streaming, and `stream` is the
    author's own name for the message being built: every call reaches the
    browser, and only the last is written to the record.
    """
    session = _session(command)
    data = command.data
    kind = data.get("kind", sessions.TEXT)
    stream = data.get("stream")
    if stream is not None and not isinstance(stream, str):
        raise ValueError("stream must be a string")

    message = session.post(
        str(data.get("agent") or session.agent or "agent"),
        kind if isinstance(kind, str) else sessions.TEXT,
        text=str(data.get("text", "")),
        mime=str(data.get("mime", "")),
        data=bytes(data.get("data", b"")),
        stream=stream,
        done=bool(data.get("done", True)),
    )
    await command.send(
        {"type": "session.posted", "session": session.id, "seq": message.seq}
    )
    await session.broadcast({"type": "session.message", "post": message.wire()})


@ws_handler(name="session.collect")
async def session_collect(command: WsCommand) -> None:
    """Everything typed since an agent last looked, plus where things stand.

    The pushed events normally get there first; this is what an agent that
    reconnected uses to catch up.
    """
    session = _session(command)
    inputs = [message.wire() for message in session.inputs]
    session.inputs.clear()
    await command.send(
        {
            "type": "session.collected",
            "session": session.id,
            "state": session.state,
            "inputs": inputs,
        }
    )


@ws_handler(name="session.close")
async def session_close(command: WsCommand) -> None:
    """The worker is done. Its exit outcome decides whether that was success."""
    session = _session(command)
    code = command.data.get("code", 0)
    if not isinstance(code, int) or isinstance(code, bool):
        raise ValueError("code must be an integer")
    for message in session.flush():
        await session.broadcast({"type": "session.message", "post": message.wire()})
    if session.live:
        session.set_state(sessions.FINISHED if code == 0 else sessions.FAILED)
    await session.broadcast({"type": "session.closed", "state": session.state})
