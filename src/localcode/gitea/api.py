"""The slice of gitea's API localcode uses."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx


class GiteaError(Exception):
    """A non-success response from Gitea, with its status available to callers."""

    def __init__(
        self,
        message: str,
        *,
        method: str = "",
        path: str = "",
        status_code: int | None = None,
        body: str = "",
    ) -> None:
        self.method = method
        self.path = path
        self.status_code = status_code
        self.body = body
        super().__init__(message)


class Gitea:
    """An authenticated client for one gitea instance."""

    def __init__(
        self,
        base_url: str,
        token: str = "",
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        headers = {"Authorization": f"token {token}"} if token else {}
        self._client = httpx.AsyncClient(
            base_url=base_url, headers=headers, timeout=30, transport=transport
        )

    async def __aenter__(self) -> Gitea:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self._client.aclose()

    async def _json(self, method: str, path: str, **kwargs: Any) -> Any:
        response = await self._client.request(method, path, **kwargs)
        if response.status_code >= 400:
            raise GiteaError(
                f"{method} {path} -> {response.status_code}: {response.text}",
                method=method,
                path=path,
                status_code=response.status_code,
                body=response.text,
            )
        return response.json() if response.content else None

    async def version(self) -> str:
        return (await self._json("GET", "/version"))["version"]

    async def wait_ready(self, timeout: float = 120.0) -> str:
        """Poll /version until gitea answers, or give up.

        Gitea runs its migrations on first start, so the first boot of a project
        takes appreciably longer than later ones.
        """
        deadline = asyncio.get_running_loop().time() + timeout
        last = ""
        while asyncio.get_running_loop().time() < deadline:
            try:
                return await self.version()
            except (httpx.HTTPError, GiteaError) as exc:
                last = str(exc)
                await asyncio.sleep(0.5)
        raise GiteaError(f"gitea did not come up within {timeout:.0f}s: {last}")

    # --- repositories --------------------------------------------------------

    async def repo(self, owner: str, name: str) -> dict | None:
        try:
            return await self._json("GET", f"/repos/{owner}/{name}")
        except GiteaError as exc:
            if exc.status_code == 404:
                return None
            raise

    async def create_repo(self, name: str, description: str = "") -> dict:
        return await self._json(
            "POST",
            "/user/repos",
            json={
                "name": name,
                "description": description,
                # Public: gitea is on loopback, and a link to a pull request
                # that 404s unless you happen to be signed in is no link.
                "private": False,
                "auto_init": False,
                "default_branch": "main",
            },
        )

    async def user_exists(self, username: str) -> bool:
        try:
            await self._json("GET", f"/users/{username}")
        except GiteaError:
            return False
        return True

    async def publish(self, owner: str, name: str) -> None:
        await self._json("PATCH", f"/repos/{owner}/{name}", json={"private": False})

    # --- pull requests -------------------------------------------------------

    async def list_pulls(self, owner: str, name: str, state: str = "open") -> list[dict]:
        try:
            return await self._json(
                "GET", f"/repos/{owner}/{name}/pulls", params={"state": state}
            )
        except GiteaError as exc:
            # Gitea reports 404 rather than [] for this endpoint while an
            # existing repository has no default branch/commit yet.
            if exc.status_code == 404 and await self.repo(owner, name) is not None:
                return []
            raise

    async def create_pull(
        self, owner: str, name: str, *, head: str, base: str, title: str, body: str = ""
    ) -> dict:
        return await self._json(
            "POST",
            f"/repos/{owner}/{name}/pulls",
            json={"head": head, "base": base, "title": title, "body": body},
        )

    # --- collaborators -------------------------------------------------------

    async def add_collaborator(self, owner: str, name: str, user: str) -> None:
        await self._json(
            "PUT",
            f"/repos/{owner}/{name}/collaborators/{user}",
            json={"permission": "write"},
        )
