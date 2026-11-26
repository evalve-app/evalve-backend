"""The HTTP API. Every route sits under /evalve so one hostname can serve Kitaru and Evalve."""

from collections.abc import Callable
from contextlib import asynccontextmanager
from http.cookiejar import DefaultCookiePolicy
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, FastAPI
from fastapi.responses import JSONResponse

from evalve_backend import __version__, database
from evalve_backend.auth import CSRF_HEADER, Account, require_account
from evalve_backend.loop_task_api import SECURITY_SCHEME as LOOP_TASK_SECRET, router as loop_task_router
from evalve_backend.loops_api import router as loops_router
from evalve_backend.loop_steps import LoopSteps
from evalve_backend.settings import Settings
from evalve_backend.steps import KitaruLoopSteps
from evalve_backend.store import PostgresStore, Store

PREFIX = "/evalve"
BEARER_ONLY = [{"HTTPBearer": []}]
# The two ways to sign in. The second needs the cookie and the header together.
SIGN_IN = [{"HTTPBearer": []}, {"SessionCookie": [], "CsrfToken": []}]


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
            # One client serves every caller. It must never keep a cookie from a Kitaru answer and
            # send it with the request of another caller, so its cookie jar accepts nothing.
            kitaru.cookies.jar.set_policy(DefaultCookiePolicy(allowed_domains=[]))
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
    app.state.session_cookie_name = settings.session_cookie_name
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
    app.include_router(loop_task_router, prefix=PREFIX)
    build_contract = app.openapi

    def contract() -> dict:
        """The API contract, with both ways to sign in on each route that needs a sign-in.

        FastAPI can only say "Bearer or cookie or header". The dashboard session needs the cookie
        and the header together, so the security of each route is written here.
        """
        first_build = app.openapi_schema is None
        schema = build_contract()
        if first_build:
            schemes = schema["components"]["securitySchemes"]
            schemes["SessionCookie"] = {
                "type": "apiKey", "in": "cookie", "name": "evalve_session",
                "description": (
                    "The dashboard session cookie of Kitaru. A deployment can change the name with "
                    "EVALVE_SESSION_COOKIE_NAME, and an empty name turns this way to sign in off."
                ),
            }
            schemes["CsrfToken"] = {
                "type": "apiKey", "in": "header", "name": CSRF_HEADER,
                "description": "The csrf_token of the Kitaru login answer. Kitaru checks it on every request.",
            }
            schemes[LOOP_TASK_SECRET] = {
                "type": "apiKey", "in": "header", "name": "Authorization",
                "description": (
                    "The secret of the Loop task, as `EvalveLoopTask <secret>`. Only the route for the "
                    "Assignments of a Loop task accepts it."
                ),
            }
            for path in schema["paths"].values():
                for operation in path.values():
                    if operation.get("security") == BEARER_ONLY:
                        operation["security"] = SIGN_IN
        return schema

    app.openapi = contract
    return app
