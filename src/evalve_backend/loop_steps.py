"""Operations the Loop API delegates to the Kitaru orchestration in sc-268."""

from dataclasses import dataclass
from typing import Protocol

from evalve_backend.auth import Account
from evalve_backend.store import RunPlan


class StepUnavailable(Exception):
    """A temporary execution failure. The caller can retry."""


class PreflightRejected(Exception):
    """The setup cannot run. Its message describes what the caller can fix."""


@dataclass(frozen=True)
class PreparedStart:
    agent_id: str
    preflight: dict
    first_run: RunPlan


class LoopSteps(Protocol):
    """Each operation uses the Authorization header for this request only."""

    async def preflight(self, config: dict, account: Account, authorization: str) -> PreparedStart: ...

    async def submit_pending(self, owner: str, loop_id: str, experiment: int, authorization: str) -> None:
        """Under the Loop lock, reconcile and submit the checkpoint's pending first Run.

        Enforce the run budget before submission. Return after recording the Kitaru
        Run ID or a terminal Loop outcome. A retry
        must leave an already submitted Run alone.
        """
        ...

    async def advance(self, owner: str, loop_id: str, authorization: str) -> None:
        """Advance the Loop under its lock. Poll an in-flight Run at most once."""
        ...

    async def cancel(self, owner: str, loop_id: str, authorization: str) -> None:
        """Under the Loop lock, reconcile any pending Run and cancel if needed.

        Commit cancellation only after it is accepted or there is no Run to stop.
        """
        ...
