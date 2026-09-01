from unittest import IsolatedAsyncioTestCase

import httpx

from localcode.gitea.api import Gitea, GiteaError


class PullListingTest(IsolatedAsyncioTestCase):
    async def test_existing_empty_repo_has_no_pulls(self) -> None:
        def respond(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/pulls"):
                return httpx.Response(404, json={"message": "target not found"})
            return httpx.Response(200, json={"name": "demo"})

        async with self.client(respond) as gitea:
            self.assertEqual(await gitea.list_pulls("localcode", "demo"), [])

    async def test_missing_repo_still_raises(self) -> None:
        def respond(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"message": "not found"})

        async with self.client(respond) as gitea:
            with self.assertRaises(GiteaError):
                await gitea.list_pulls("localcode", "missing")

    async def test_repo_does_not_hide_server_errors(self) -> None:
        def respond(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"message": "broken"})

        async with self.client(respond) as gitea:
            with self.assertRaises(GiteaError):
                await gitea.repo("localcode", "demo")

    def client(self, handler) -> Gitea:
        return Gitea(
            "http://gitea.test/api/v1",
            transport=httpx.MockTransport(handler),
        )
