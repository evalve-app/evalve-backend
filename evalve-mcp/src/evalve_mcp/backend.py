"""Hand-written HTTP calls to the Evalve backend: the key, the time limits, the repeats and the messages."""

import asyncio
from collections.abc import Awaitable, Callable
from urllib.parse import quote, urlsplit
from uuid import uuid4

import httpx

# The backend runs the preflight and starts a run before it answers a start or a proposal.
# The 60 seconds is an estimate with no measurement.
SLOW_SECONDS = 60
FAST_SECONDS = 15
REPEAT_DELAYS = (2, 5)
INSTALL = 'uv add "evalve-mcp @ git+https://github.com/evalve-app/evalve-backend@v{version}#subdirectory=evalve-mcp"'

NO_KEY = (
    "KITARU_API_KEY is not set. Set KITARU_API_KEY to your Kitaru API key in the environment "
    "of the coding agent. Then start the coding agent again."
)
BAD_KEY = "The backend refused the key (401). Make sure that KITARU_API_KEY is a valid Kitaru API key for this server."
NOT_LOST = "The Loop is not lost. Call evalve_loop_status again in a minute."
NAME_IN_USE = "Call evalve_loop_list to find the Loop that has this name."


class BackendError(Exception):
    """A message for the coding agent."""


def checked_server_url(url: str) -> str:
    """Return the backend URL with no end slash. Raises ValueError if the URL is not safe to use."""
    problem = f"--server must be the URL of the Evalve backend, for example https://<host>/evalve. Got {url!r}."
    try:
        target = urlsplit(url)
        host = target.hostname
    except ValueError:
        raise ValueError(problem) from None
    if not host or target.scheme not in ("http", "https") or target.query or target.fragment or target.username:
        raise ValueError(problem)
    if target.scheme == "http" and host not in ("localhost", "127.0.0.1", "::1"):
        raise ValueError("--server must use HTTPS unless the host is localhost.")
    return url.rstrip("/")


class Backend:
    """Each method calls one backend route and returns the text that the tool gives to the coding agent."""

    def __init__(
        self, server_url: str, key: str | None, *, version: str,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self.server_url = checked_server_url(server_url)
        self.version = version
        self.warning: str | None = None
        self._key = key or None
        self._sleep = sleep
        self._client = httpx.AsyncClient(transport=transport)
        self._account_id: str | None = None

    async def close(self) -> None:
        await self._client.aclose()

    async def read_backend_version(self) -> None:
        """Compare the backend version with this package. A health call with no usable answer gives no warning."""
        try:
            response = await self._client.get(f"{self.server_url}/health", timeout=FAST_SECONDS)
            theirs = response.json()["version"]
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            return
        if isinstance(theirs, str) and theirs.split(".")[:2] != self.version.split(".")[:2]:
            self.warning = (
                f"Warning: evalve-mcp {self.version} does not agree with the backend {theirs}. "
                f"Install the correct version: {INSTALL.format(version=theirs)}"
            )

    async def start_loop(self, setup: dict) -> str:
        # One key for each tool call. The repeats of this call send the same key, so they make one Loop.
        headers = {"Idempotency-Key": str(uuid4())}
        return await self._call("POST", "/v1/loops", body=setup, headers=headers, timeout=SLOW_SECONDS,
                                hints={409: NAME_IN_USE})

    async def loop_status(self, loop_id: str) -> str:
        return await self._call("GET", f"/v1/loops/{_segment(loop_id)}")

    async def list_loops(self, status: str | None, cursor: str | None) -> str:
        async def own_loops() -> str:
            if self._account_id is None:
                self._account_id = (await self._send("GET", "/v1/me")).json()["id"]
            params = {"owner_account_id": self._account_id, "status": status, "cursor": cursor}
            params = {name: value for name, value in params.items() if value is not None}
            return (await self._send("GET", "/v1/loops", params=params)).text

        return await self._message(own_loops())

    async def propose_experiment(self, loop_id: str, proposal: dict) -> str:
        return await self._call("POST", f"/v1/loops/{_segment(loop_id)}/experiments", body=proposal, timeout=SLOW_SECONDS)

    async def cancel_loop(self, loop_id: str) -> str:
        return await self._call("POST", f"/v1/loops/{_segment(loop_id)}/cancel")

    async def run_evidence(self, loop_id: str, experiment: int, run: int) -> str:
        return await self._call("GET", f"/v1/loops/{_segment(loop_id)}/experiments/{experiment}/runs/{run}")

    async def _call(self, method: str, path: str, **options) -> str:
        async def body() -> str:
            return (await self._send(method, path, **options)).text

        return await self._message(body())

    async def _message(self, result: Awaitable[str]) -> str:
        try:
            text = await result
        except BackendError as error:
            text = str(error)
        return f"{self.warning}\n{text}" if self.warning else text

    async def _send(
        self, method: str, path: str, *, body: dict | None = None, params: dict | None = None,
        headers: dict | None = None, timeout: float = FAST_SECONDS, hints: dict[int, str] | None = None,
    ) -> httpx.Response:
        if self._key is None:
            raise BackendError(NO_KEY)
        headers = {**(headers or {}), "Authorization": f"Bearer {self._key}"}
        failure = ""
        for delay in (*REPEAT_DELAYS, None):
            try:
                response = await self._client.request(
                    method, self.server_url + path, json=body, params=params, headers=headers, timeout=timeout,
                )
            except httpx.TransportError:
                failure = (
                    f"The backend at {self.server_url} did not answer. {NOT_LOST} If the server URL changed, "
                    "tell the Teammate to run: evalve-mcp setup --server <URL>"
                )
            else:
                if response.status_code != 503:
                    return _accepted(response, hints or {})
                failure = f"The backend cannot answer now (503). {NOT_LOST}"
            if delay is not None:
                await self._sleep(delay)
        raise BackendError(failure)


def _accepted(response: httpx.Response, hints: dict[int, str]) -> httpx.Response:
    """Return a successful answer. Each other answer becomes a message, with no repeat."""
    if response.is_success:
        return response
    if response.status_code == 401:
        raise BackendError(BAD_KEY)
    raise BackendError(" ".join(filter(None, (_detail(response), hints.get(response.status_code)))))


def _segment(value: str) -> str:
    return quote(value, safe="")


def _detail(response: httpx.Response) -> str:
    """The backend message, with no change when the backend gives one."""
    try:
        detail = response.json()["detail"]
    except (ValueError, KeyError, TypeError):
        detail = None
    if isinstance(detail, str) and detail:
        return detail
    return f"The backend answered {response.status_code}: {response.text}"
