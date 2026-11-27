"""Request and response schemas for the v1 Loop API."""

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, StrictBool, StrictInt

LoopStatus = Literal["running", "completed", "failed", "cancelled"]
LoopOutcome = Literal["passed", "already_passing", "max_runs", "budget_reached", "stopped_idle", "stopped_by_user", "error"]
NextAction = Literal["start_control", "resume_start", "collect", "replay", "retry", "needs_change",
                     "stop_success", "stop_attempt_cap", "stop_budget", "stop_idle", "stop_incomplete"]
ChangeType = Literal["system_prompt", "model"]
RunOutcome = Literal["red", "green", "incomplete"]
Nonempty = Annotated[str, Field(min_length=1)]
Money = Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Evaluator(Input):
    ref: Nonempty
    params: dict[str, JsonValue] = Field(default_factory=dict)
    connection_id: Nonempty | None = None


class ReplayPolicy(BaseModel):
    model_config = ConfigDict(extra="allow")

    type: Nonempty
    scope: str | None = None
    on_miss: Nonempty | None = None


class ToolPolicy(Input):
    default: ReplayPolicy
    tools: dict[str, ReplayPolicy] = Field(default_factory=dict)


class StartLoop(Input):
    name: Nonempty
    cohort_version_id: Nonempty
    evaluators: list[Evaluator]
    tool_policy: ToolPolicy
    allowed_changes: list[ChangeType]
    allowed_models: list[Nonempty]
    required_consecutive_greens: StrictInt
    max_runs: StrictInt
    budget_usd: Money
    agent_version: Nonempty | None = None
    control_model: Nonempty | None = None
    control_prompt: str | None = None
    control_estimate_usd: Money | None = None
    hypothesis: str | None = None
    allow_live_tools: StrictBool = False


class ProposeExperiment(Input):
    experiment: Annotated[StrictInt, Field(ge=1)]
    change: Nonempty
    system_prompt: Nonempty | None = None
    model: Nonempty | None = None
    estimate_usd: Money | None = None
    control_model: Nonempty | None = None


class ApiError(BaseModel):
    detail: str


class LoopSummary(BaseModel):
    loop_id: str
    name: str
    slug: str
    agent_id: str
    owner_account_id: str
    status: LoopStatus
    outcome: LoopOutcome | None
    reason: str | None
    created_at: datetime
    updated_at: datetime


class RunResult(BaseModel):
    run_id: str
    run_status: str
    run_error: str | None = None
    # The tasks of the Run that a worker outside the Loop ran. Only a Run that failed the `worker_id` check has it.
    foreign_tasks: int | None = None
    outcome: RunOutcome
    passed: int
    failed: int
    unscored: int
    missing_session_ids: list[str]
    recorded_cost_usd: Decimal
    cost_incomplete: bool


class RunSummary(BaseModel):
    number: int
    run_id: str | None
    cohort_version_id: str
    agent_version_id: str
    estimated_cost_usd: Decimal
    submit_started_at: datetime
    collected_at: datetime | None
    result: RunResult | None


class EvaluationEvidence(BaseModel):
    id: str | None
    passed: bool | None
    explanation: str | None


class CaseEvidence(BaseModel):
    case: str
    baseline_session_id: str
    replay_id: str
    replay_status: str
    result_session_id: str | None
    result_status: str | None
    error: str | None
    passed: bool | None
    evaluations: dict[str, EvaluationEvidence]
    reply: JsonValue
    cost_usd: Decimal | None


class RunDetail(RunSummary):
    loop_id: str
    experiment_number: int
    evidence: list[CaseEvidence] | None
    failed_cases: list[CaseEvidence]


class ExperimentSummary(BaseModel):
    number: int
    name: str
    experiment_id: str | None
    override: dict[ChangeType, str]
    change: str
    runs: list[RunSummary]


class ExperimentAccepted(BaseModel):
    experiment_id: str


class WinningChange(BaseModel):
    experiment: int
    experiment_id: str | None
    override: dict[ChangeType, str]
    change: str


class Spend(BaseModel):
    recorded_usd: Decimal
    accounted_usd: Decimal
    budget_usd: Decimal


class RunCount(BaseModel):
    used: int
    max: int


class LoopNext(BaseModel):
    action: NextAction
    runs: int
    recorded_cost_usd: Decimal
    accounted_cost_usd: Decimal
    green_streak: int
    experiment: int | None = None
    run_id: str | None = None
    reason: str | None = None


class NeedsChange(BaseModel):
    experiment: int
    allowed_changes: list[ChangeType]
    allowed_models: list[str]
    overrides_tried: list[dict[ChangeType, str]]
    failed_cases: list[CaseEvidence]
    idle_deadline: datetime | None


class LoopListItem(LoopSummary):
    """One row of the Loop list. It has what the dashboard list shows, so the list needs no other call."""

    hypothesis: str | None
    experiments: int = Field(description="The number of Experiments of the Loop. The Control Experiment counts.")
    spend: Spend
    next_action: NextAction | None = Field(
        description="The next action of a running Loop, as in the status call. Null for a Loop at an end state.",
    )


class LoopList(BaseModel):
    items: list[LoopListItem]
    next_cursor: str | None


class LoopDetail(LoopSummary):
    setup: StartLoop
    next: LoopNext | None
    needs: NeedsChange | None
    spend: Spend
    runs: RunCount
    experiments: list[ExperimentSummary]
    winner: WinningChange | None


class LoopReport(LoopSummary):
    spend: Spend
    runs: RunCount
    winner: WinningChange | None


class TaskPoll(BaseModel):
    """The poll of a Supervisor. A later contract version can add a field, and the backend must
    then refuse the version, not the body. Thus this body permits fields that it does not know."""

    # The limit is the size of the column. The backend stores a version that it refuses.
    contract_version: Annotated[StrictInt, Field(ge=0, le=2**31 - 1)]
    last: StrictBool = False


class Assignment(BaseModel):
    run_id: str
    job_id: str
    worker_id: str
    worker_token: str


class TaskPollReply(BaseModel):
    run_deadline: datetime | None
    assignments: list[Assignment]
