"""The stdio MCP server. Each tool calls one backend route and has no Loop rules."""

import argparse
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib.metadata import version as package_version
from typing import Annotated, Any, Literal

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, ConfigDict, Field

from evalve_mcp.backend import Backend, checked_server_url
from evalve_mcp.setup import SetupError, write_entry

ChangeType = Literal["system_prompt", "model"]
LoopStatus = Literal["running", "completed", "failed", "cancelled"]
Money = Annotated[float | str, Field(description="An amount in US dollars, as a number or a decimal string.")]

POLL = (
    "No tool waits for a Loop to end. Call evalve_loop_status every minute or so. "
    "When the next action is `needs_change`, read the failed cases and call evalve_loop_propose_experiment. "
    "A Loop that waits for a change for more than 24 hours ends with the outcome `stopped_idle`. "
    "The outcome `already_passing` means that the agent passed with no change, so there is nothing to propose."
)


class Evaluator(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ref: str = Field(description="The Kitaru evaluator reference.")
    params: dict[str, Any] | None = None
    connection_id: str | None = None


class ReplayPolicy(BaseModel):
    model_config = ConfigDict(extra="allow")

    type: str = Field(description="`static` or `history`. A Loop refuses `passthrough` and `llm`.")
    scope: str | None = Field(
        default=None,
        description=(
            "Necessary for `history`, and it must be `baseline`: a Replay reads only the tool results of the "
            "session that it replays. A Loop refuses `cohort_version` and `agent`."
        ),
    )
    on_miss: str | None = Field(
        default=None,
        description=(
            "What a `history` lookup does when no recorded result matches: `fail` or `error_result`. "
            "With `fail`, the Replay stops and the Run is incomplete. Two incomplete Runs in a row end the Loop. "
            "With `error_result`, the agent gets a tool error and the Loop continues."
        ),
    )


class ToolPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    default: ReplayPolicy = Field(description="The replay policy for each tool that `tools` does not name.")
    tools: dict[str, ReplayPolicy] | None = Field(
        default=None, description="The replay policy for one tool, by the name of the tool.",
    )


def _body(**fields: Any) -> dict:
    """The API body: the tool parameters with the same names. A parameter with no value is not sent."""
    body = {}
    for name, value in fields.items():
        if isinstance(value, list):
            value = [item.model_dump(exclude_none=True) if isinstance(item, BaseModel) else item for item in value]
        elif isinstance(value, BaseModel):
            value = value.model_dump(exclude_none=True)
        if value is not None:
            body[name] = value
    return body


def build_server(backend: Backend) -> FastMCP:
    @asynccontextmanager
    async def lifespan(_server: FastMCP) -> AsyncIterator[None]:
        await backend.read_backend_version()
        try:
            yield
        finally:
            await backend.close()

    server = FastMCP("evalve", lifespan=lifespan)

    def tool(description: str):
        return server.tool(description=description, structured_output=False)

    @tool(
        "Start a Loop on the Evalve backend and return it at once. The backend checks the setup "
        "and starts the Control Experiment. Call this tool one time for each Loop. " + POLL
    )
    async def evalve_loop_start(
        name: Annotated[str, Field(description="The Loop name. It must be unique for the agent.")],
        cohort_version_id: Annotated[str, Field(description="The exact UUID of the Kitaru cohort version.")],
        evaluators: list[Evaluator],
        tool_policy: ToolPolicy,
        allowed_changes: Annotated[list[ChangeType], Field(description="The types of change that an Experiment can make.")],
        allowed_models: Annotated[list[str], Field(description="The model IDs that a `model` change can use.")],
        required_consecutive_greens: Annotated[int, Field(description="The number of green runs in sequence that a change must get to pass.")],
        max_runs: Annotated[int, Field(description="The maximum number of runs for the full Loop.")],
        budget_usd: Money,
        agent_version: str | None = None,
        control_model: str | None = None,
        control_prompt: str | None = None,
        control_estimate_usd: Money | None = None,
        hypothesis: str | None = None,
        allow_live_tools: bool = False,
    ) -> str:
        return await backend.start_loop(_body(
            name=name, cohort_version_id=cohort_version_id, evaluators=evaluators, tool_policy=tool_policy,
            allowed_changes=allowed_changes, allowed_models=allowed_models,
            required_consecutive_greens=required_consecutive_greens, max_runs=max_runs, budget_usd=budget_usd,
            agent_version=agent_version, control_model=control_model, control_prompt=control_prompt,
            control_estimate_usd=control_estimate_usd, hypothesis=hypothesis, allow_live_tools=allow_live_tools,
        ))

    @tool(
        "Read one Loop: the status, the outcome, the next action and each Experiment with its Kitaru experiment id. "
        "When the next action is `needs_change`, `needs` has the failed cases and `idle_deadline`, "
        "the time at which the idle limit ends the Loop. When a change passes, `winner` has the change. "
        "For the owner, this call also moves a running Loop forward one step. " + POLL
    )
    async def evalve_loop_status(loop_id: str) -> str:
        return await backend.loop_status(loop_id)

    @tool(
        "List your Loops, newest first. Use this tool in a new session to find a Loop from an earlier session. "
        "To get the next page, send `next_cursor` from the result as `cursor`."
    )
    async def evalve_loop_list(status: LoopStatus | None = None, cursor: str | None = None) -> str:
        return await backend.list_loops(status, cursor)

    @tool(
        "Send one change for the next Experiment of a Loop and return the Kitaru experiment id. "
        "Call this tool only when the next action of the Loop is `needs_change`. "
        "Send `system_prompt` or `model`. The backend starts the first run of the Experiment. "
        "Then call evalve_loop_status every minute or so."
    )
    async def evalve_loop_propose_experiment(
        loop_id: str,
        experiment: Annotated[int, Field(description="The Experiment number from `needs.experiment` in the Loop status.")],
        change: Annotated[str, Field(description="One sentence that tells a person what the change is and why.")],
        system_prompt: Annotated[str | None, Field(description="The full new system prompt.")] = None,
        model: Annotated[str | None, Field(description="A model ID from `needs.allowed_models`.")] = None,
        estimate_usd: Money | None = None,
        control_model: str | None = None,
    ) -> str:
        return await backend.propose_experiment(loop_id, _body(
            experiment=experiment, change=change, system_prompt=system_prompt, model=model,
            estimate_usd=estimate_usd, control_model=control_model,
        ))

    @tool("Cancel a Loop that you started and return it. A Loop that ended stays as it is.")
    async def evalve_loop_cancel(loop_id: str) -> str:
        return await backend.cancel_loop(loop_id)

    @tool(
        "Read one run of one Experiment: each case, the failed cases and the evaluator explanations. "
        "Use the failed cases to select the next change. Experiment 0 is the Control Experiment. The first run is 1."
    )
    async def evalve_loop_run_evidence(loop_id: str, experiment: int, run: int) -> str:
        return await backend.run_evidence(loop_id, experiment, run)

    return server


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="evalve-mcp", description="Run the Evalve MCP server on stdio, or write its entry in .mcp.json.",
    )
    parser.add_argument("command", nargs="?", choices=["setup"], help="Write the `evalve` entry in ./.mcp.json.")
    parser.add_argument("--server", required=True, help="The URL of the Evalve backend, for example https://<host>/evalve.")
    options = parser.parse_args(argv)
    try:
        server_url = checked_server_url(options.server)
        if options.command == "setup":
            path = write_entry(server_url)
            print(f"Wrote the `evalve` entry in {path}.")
            return
    except (ValueError, SetupError) as error:
        sys.exit(f"evalve-mcp: {error}")
    backend = Backend(server_url, os.environ.get("KITARU_API_KEY"), version=package_version("evalve-mcp"))
    build_server(backend).run()


if __name__ == "__main__":
    main()
