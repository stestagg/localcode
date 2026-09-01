"""Standing gitea up for a project, driven from the host.

The container generates nothing: app.ini is written here before the hub starts,
and the primary accounts, tokens and master repo are created here once it
answers. All of it is idempotent, so a second `localcode run` only checks.
"""

from __future__ import annotations

import asyncio
import secrets

from .. import repo as git
from ..docker import client
from ..project import GITEA_HUMAN, GITEA_OWNER, Project
from .api import Gitea

AUTOMATION_USER = GITEA_OWNER
HUMAN_USER = GITEA_HUMAN
REMOTE = "localcode"

# Container-side paths. /data is the symlink the hub entrypoint makes to the
# project's .localcode/state/.
WORK_DIR = "/data/gitea"
CONF = f"{WORK_DIR}/conf/app.ini"

APP_INI = """\
APP_NAME  = localcode
RUN_USER  = gitea
RUN_MODE  = prod
WORK_PATH = {work_dir}

[server]
PROTOCOL         = http
; Only caddy, in the same container, talks to gitea.
HTTP_ADDR        = 127.0.0.1
HTTP_PORT        = 3000
ROOT_URL         = {root_url}
APP_DATA_PATH    = {work_dir}/data
DISABLE_SSH      = true
LFS_START_SERVER = true

[database]
DB_TYPE             = sqlite3
PATH                = {work_dir}/data/gitea.db
SQLITE_JOURNAL_MODE = WAL

[repository]
ROOT           = {work_dir}/repos
DEFAULT_BRANCH = main
; Alpine has no bash, and this is what gitea puts in the hooks it writes.
SCRIPT_TYPE    = sh

[repository.upload]
TEMP_PATH = {work_dir}/tmp/uploads

[git]
HOME_PATH = {work_dir}/home

[log]
MODE      = file
ROOT_PATH = {work_dir}/log
LEVEL     = info

[security]
INSTALL_LOCK = true
SECRET_KEY   = {secret_key}

[service]
DISABLE_REGISTRATION = true
REQUIRE_SIGNIN_VIEW  = false
; Caddy is the only thing that can reach gitea, so it is the one that says who
; you are: it stamps X-WEBAUTH-USER on browser requests and strips it from any
; request that brought its own credentials. See the hub's Caddyfile.
ENABLE_REVERSE_PROXY_AUTHENTICATION = true

[session]
PROVIDER        = file
PROVIDER_CONFIG = {work_dir}/data/sessions

[cron.update_checker]
ENABLED = false
"""


def write_config(project: Project) -> None:
    """Lay out state/gitea/ and write app.ini, once per project.

    INTERNAL_TOKEN, JWT_SECRET and LFS_JWT_SECRET are left out on purpose:
    gitea generates them on first start and writes them back here itself.
    """
    (project.gitea_dir / "conf").mkdir(parents=True, exist_ok=True)
    if project.gitea_conf.exists():
        return
    project.gitea_conf.write_text(
        APP_INI.format(
            work_dir=WORK_DIR,
            root_url=project.gitea_url,
            secret_key=secrets.token_urlsafe(32),
        )
    )


async def _gitea_cli(project: Project, *args: str) -> str:
    """Run gitea's own CLI inside the hub, as the user that owns the data."""
    return await client.check(
        "exec",
        "-u",
        "gitea",
        "-e",
        f"GITEA_WORK_DIR={WORK_DIR}",
        project.hub_container,
        "gitea",
        *args,
        "--config",
        CONF,
    )


async def _create_account(project: Project, username: str, *, admin: bool = False) -> str:
    """Create a gitea user, returning the password it was given."""
    password = secrets.token_urlsafe(18)
    args = [
        "admin",
        "user",
        "create",
        "--username",
        username,
        "--password",
        password,
        "--email",
        f"{username}@localcode.local",
        "--must-change-password=false",
    ]
    if admin:
        args.append("--admin")
    await _gitea_cli(project, *args)
    return password


async def _replace_password(project: Project, username: str) -> str:
    """Replace a user's unknown password and return the new value."""
    password = secrets.token_urlsafe(18)
    await _gitea_cli(
        project,
        "admin",
        "user",
        "change-password",
        "--username",
        username,
        "--password",
        password,
        "--must-change-password=false",
    )
    return password


async def _mint_token(project: Project, username: str) -> str:
    """An access token for `username`.

    Token names have to be unique per user, so each one gets its own -- an agent
    whose token was lost can be given another without tripping over the old.
    """
    output = await _gitea_cli(
        project,
        "admin",
        "user",
        "generate-access-token",
        "--username",
        username,
        "--token-name",
        f"localcode-{secrets.token_hex(4)}",
        "--scopes",
        "all",
        "--raw",
    )
    # --raw still prints whatever warnings gitea has about its own config
    # first, so the token is the last line rather than the whole of stdout.
    return output.splitlines()[-1].strip()


async def sync_personas(project: Project, gitea: Gitea) -> list[str]:
    """Give every named persona a gitea account and a token.

    Personas are only ever added. A definition that goes away leaves its account
    behind, because deleting it would take the pull requests it authored with
    it. Returns the persona names this call had to create.
    """
    added: list[str] = []
    for persona in project.personas():
        name = persona.name
        if name in project.runtime.personas and await gitea.user_exists(name):
            continue
        if not await gitea.user_exists(name):
            await _create_account(project, name)
        project.runtime.personas[name] = await _mint_token(project, name)
        # Agents push branches and open pull requests, so they need write.
        await gitea.add_collaborator(project.gitea_owner, project.gitea_repo, name)
        added.append(name)
    return added


async def _ensure_primary_accounts(project: Project, gitea: Gitea) -> None:
    """Provision the distinct automation and interactive identities.

    `localcode` owns the repository and its token authenticates controller
    operations. Browser traffic is reverse-proxy-authenticated as the separate
    `human` administrator, whose password is retained for direct sign-in.
    """
    if not await gitea.user_exists(AUTOMATION_USER):
        await _create_account(project, AUTOMATION_USER, admin=True)
        project.runtime.automation_token = ""
    if not project.runtime.automation_token:
        project.runtime.automation_token = await _mint_token(
            project, AUTOMATION_USER
        )

    if not await gitea.user_exists(HUMAN_USER):
        project.runtime.human_password = await _create_account(
            project, HUMAN_USER, admin=True
        )
    elif not project.runtime.human_password:
        # Covers migration from the old single-account layout and recovery
        # from a runtime write interrupted after the account was created.
        project.runtime.human_password = await _replace_password(
            project, HUMAN_USER
        )


async def provision(project: Project) -> None:
    """Bring gitea from "just started" to "has the master repo", idempotently."""
    async with Gitea(project.gitea_api) as anonymous:
        await anonymous.wait_ready()
        await _ensure_primary_accounts(project, anonymous)
        project.save_runtime()

    async with Gitea(project.gitea_api, project.runtime.automation_token) as gitea:
        existing = await gitea.repo(project.gitea_owner, project.gitea_repo)
        if existing is None:
            await gitea.create_repo(project.gitea_repo, f"localcode: {project.name}")
        elif existing["private"]:
            await gitea.publish(project.gitea_owner, project.gitea_repo)
        await sync_personas(project, gitea)

    project.save_runtime()
    # Fetch, rebase and push against gitea, all of it blocking git. On the loop
    # it would stall the signal handler for as long as it takes.
    await asyncio.to_thread(sync_master, project)


def sync_master(project: Project) -> None:
    """Publish main and the separate metadata branch to gitea."""
    git.set_remote(
        project.path,
        REMOTE,
        project.push_url(AUTOMATION_USER, project.runtime.automation_token),
    )
    if git.has_commits(project.path):
        git.update_mirror(project.path, REMOTE)


async def sync_metadata(project: Project) -> None:
    """Publish host metadata and provision accounts for new valid personas."""
    await asyncio.to_thread(sync_master, project)
    async with Gitea(project.gitea_api, project.runtime.automation_token) as gitea:
        await sync_personas(project, gitea)
    project.save_runtime()
