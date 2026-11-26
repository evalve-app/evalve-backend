"""Operations the Loop API delegates to the Kitaru orchestration in sc-268."""

from dataclasses import dataclass
from typing import Protocol

from evalve_backend.auth import Account, Credential
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

    async def preflight(self, config: dict, account: Account, credential: Credential) -> PreparedStart: ...

    async def submit_pending(self, owner: str, loop_id: str, experiment: int, credential: Credential) -> None:
        """Under the Loop lock, reconcile and submit the checkpoint's pending first Run.

        Enforce the run budget before submission. Return after recording the Kitaru
        Run ID or a terminal Loop outcome. A retry
        must leave an already submitted Run alone.
        """
        ...

    async def advance(self, owner: str, loop_id: str, credential: Credential) -> None:
        """Drive the Loop under its lock: refresh it, then start the next Run if one is due.

        Poll an in-flight Run at most once.
        """
        ...

    async def refresh(self, owner: str, loop_id: str, credential: Credential) -> None:
        """Refresh the Loop under its lock: record a finished Run and apply an end state.

        Ask Kitaru about an in-flight Run at most once. Start no Run and add no Run row.
        """
        ...

    async def delete_workers(self, owner: str, loop_id: str, credential: Credential) -> None:
        """Under the Loop lock, delete from Kitaru each worker of the Loop that no Run needs.

        For a call from the Loop owner that does not drive or refresh the Loop: the Loop ended, or
        it waits for a change. A worker that Kitaru cannot delete now never fails the call.
        """
        ...

    async def cancel(self, owner: str, loop_id: str, credential: Credential) -> None:
        """Under the Loop lock, reconcile any pending Run and cancel if needed.

        Commit cancellation only after it is accepted or there is no Run to stop.
        """
        ...
