"""The HTTP API. Every route sits under /evalve so one hostname can serve Kitaru and Evalve."""

from collections.abc import Callable
from contextlib import asynccontextmanager
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from evalve_backend import __version__, database
from evalve_backend.auth import Account, KitaruUnavailable, NotSignedIn, current_account
from evalve_backend.settings import Settings

PREFIX = "/evalve"


async def require_account(
    request: Request, authorization: Annotated[str | None, Header()] = None
) -> Account:
    """Resolve the caller through Kitaru, or refuse the request."""
    try:
        return await current_account(request.app.state.kitaru, authorization)
    except NotSignedIn:
        raise HTTPException(
            status_code=401,
            detail="Sign in to Kitaru and send its credential as a Bearer token.",
            headers={"WWW-Authenticate": "Bearer"},
        ) from None
    except KitaruUnavailable:
        raise HTTPException(
            status_code=503, detail="Kitaru could not confirm who is calling. Try again."
        ) from None


def create_app(
    settings: Settings,
    *,
    kitaru_transport: httpx.AsyncBaseTransport | None = None,
    database_ping: Callable[[Settings], bool] = database.ping,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # trust_env is off so a proxy variable can never reroute a credential.
        async with httpx.AsyncClient(
            base_url=settings.kitaru_url, timeout=10, follow_redirects=False,
            trust_env=False, transport=kitaru_transport,
        ) as kitaru:
            app.state.kitaru = kitaru
            yield

    app = FastAPI(
        title="Evalve backend", version=__version__, lifespan=lifespan,
        docs_url=f"{PREFIX}/docs", openapi_url=f"{PREFIX}/openapi.json", redoc_url=None,
    )
    router = APIRouter(prefix=PREFIX)

    @router.get("/health")
    def health():
        """Unauthenticated. Says whether the backend can reach its database, and which build is running.

        A deployment reads `image` to confirm that the exact image it asked for is the one serving.
        """
        running = {"version": __version__, "image": settings.image}
        if database_ping(settings):
            return {"status": "ok", "database": "ok", **running}
        return JSONResponse(
            {"status": "unavailable", "database": "unavailable", **running}, status_code=503
        )

    @router.get("/v1/me")
    async def me(account: Annotated[Account, Depends(require_account)]) -> Account:
        """The Kitaru account that the caller's credential belongs to."""
        return account

    app.include_router(router)
    return app
