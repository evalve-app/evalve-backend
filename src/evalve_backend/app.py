"""The HTTP API. Every route sits under /evalve so one hostname can serve Kitaru and Evalve."""

from collections.abc import Callable
from contextlib import asynccontextmanager
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, FastAPI
from fastapi.responses import JSONResponse

from evalve_backend import __version__, database
from evalve_backend.auth import Account, require_account
from evalve_backend.loops_api import router as loops_router
from evalve_backend.loop_steps import LoopSteps
from evalve_backend.settings import Settings
from evalve_backend.steps import KitaruLoopSteps
from evalve_backend.store import PostgresStore, Store

PREFIX = "/evalve"


def create_app(
    settings: Settings,
    *,
    kitaru_transport: httpx.AsyncBaseTransport | None = None,
    database_ping: Callable[[Settings], bool] = database.ping,
    store: Store | None = None,
    loop_steps: LoopSteps | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # trust_env is off so a proxy variable can never reroute a credential.
        async with httpx.AsyncClient(
            base_url=settings.kitaru_url, timeout=10, follow_redirects=False,
            trust_env=False, transport=kitaru_transport,
        ) as kitaru:
            app.state.kitaru = kitaru
            if loop_steps is None:
                # The Loop steps call the same Kitaru server with each caller's own key.
                app.state.loop_steps = KitaruLoopSteps(kitaru, app.state.loop_store, settings.kitaru_url)
            yield

    app = FastAPI(
        title="Evalve backend", version=__version__, lifespan=lifespan,
        docs_url=f"{PREFIX}/docs", openapi_url=f"{PREFIX}/openapi.json", redoc_url=None,
    )
    app.state.loop_store = PostgresStore(settings) if store is None else store
    app.state.loop_steps = loop_steps
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
    app.include_router(loops_router, prefix=PREFIX)
    return app
