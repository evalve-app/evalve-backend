"""The route from which a Loop task reads its Assignments.

The credential is the secret of the Loop task, a third type next to the Bearer credential and the
dashboard session. This route accepts no other credential, and no other route accepts the secret.
The route makes no call to Kitaru and takes no Loop lock. It never logs the secret or a worker token.
"""

from asyncio import to_thread
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request

from evalve_backend import loop_models as models, loop_tasks
from evalve_backend.loops_api import LoopRoute, LoopStore, errors

SECURITY_SCHEME = "LoopTaskSecret"


def _refused() -> HTTPException:
    # One answer for each refusal, so the reply shows no reason.
    return HTTPException(
        status_code=401, detail="The backend refused the credential of the Loop task.",
        headers={"WWW-Authenticate": loop_tasks.SCHEME},
    )


def _secret_hash(request: Request) -> str:
    secret = loop_tasks.read_secret(request.headers.get("authorization"))
    if secret is None:
        raise _refused()
    return loop_tasks.hash_secret(secret)


class LoopTaskRoute(LoopRoute):
    async def check_caller(self, request: Request, problem: dict) -> None:
        # The poll checks the secret in the handler, and an invalid body never reaches the handler.
        # Check the secret here, so that a caller with no good secret learns nothing about the body.
        store = request.app.state.loop_store
        accepted = await to_thread(store.accepts_loop_task, request.path_params["loop_id"], _secret_hash(request))
        if not accepted:
            raise _refused()


router = APIRouter(
    prefix="/v1/loops", tags=["Loop tasks"], route_class=LoopTaskRoute, responses=errors(401, 409, 422),
)


@router.post(
    "/{loop_id}/task/poll", response_model=models.TaskPollReply,
    openapi_extra={"security": [{SECURITY_SCHEME: []}]},
)
async def poll_loop_task(loop_id: str, body: models.TaskPoll, request: Request, store: LoopStore):
    """The Assignments of the open Run of the Loop, for the Supervisor in the Loop task of the Loop.

    The poll reads state: a second poll gets the same Assignments. `run_deadline` is the timeout of
    each worker process. A poll with `last` set to true ends the secret of the Loop task.
    """
    reason = loop_tasks.refused_version_reason(body.contract_version)
    poll = await to_thread(
        store.record_poll, loop_id, _secret_hash(request), body.contract_version, datetime.now(timezone.utc),
        last=body.last, refused_reason=reason,
    )
    if poll is None:
        raise _refused()
    if reason is not None:
        raise HTTPException(409, reason)
    return models.TaskPollReply(run_deadline=poll.run_deadline, assignments=list(poll.assignments))
