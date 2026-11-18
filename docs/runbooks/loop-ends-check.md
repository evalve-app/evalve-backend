# End state check: each end state of a Loop, on the backend on AWS

This runbook does rows 1 to 6 and 8 to 10 of the story [sc-345](https://app.shortcut.com/team-4-1/story/345). The reader is the coding agent of a Teammate. The coding agent calls the `evalve_*` tools of the Evalve MCP, which is connected to the Evalve backend on AWS.

Row 7 (the idle limit) uses the automated tests. It needs no Loop on AWS.

The story sc-345 has the ten rows and the expected results. Where this runbook and the story differ, tell the Teammate.

| Row | What it shows | Where |
|---|---|---|
| 8, then 1 | An Experiment that does not pass. The run limit. | Loop A |
| 2 | The budget stop | Loop B |
| 3 | The Loop failure | Loop C |
| 5 | The Control Experiment passes | Loop D |
| 6 | The cancel | Loop E |
| 4 | An Experiment passes | The demo Loop, or Loop F |
| 9 | Live tools | A start that the backend refuses |
| 10 | A budget below one Run estimate | A start that the backend refuses |
| 7 | The idle limit | Three automated tests |

Do Loops A, B and C first. They give rows 1, 2, 3 and 8, which are the most important rows.

## What you need

- The Evalve MCP, connected to the Evalve backend on AWS. The [README](../../README.md) gives the setup steps. Install the same version as the backend.
- `KITARU_API_KEY`: the Teammate's own Kitaru API key, in the environment. All Loops of this check must have the same Loop owner.
- `KITARU_URL`: the Kitaru server on AWS, with no slash at the end.
- `EVALVE_URL`: the Evalve backend, for example `https://<host>/evalve`. It is the `--server` value of the Evalve MCP.
- A Kitaru worker that is connected to the Kitaru server, so that Runs execute.
- `curl` and `jq`.
- Two fixture sets from the Teammate. A fixture set is a cohort version ID, one evaluator reference (`name@version`) and a replay tool policy.
  - **The set that fails:** the agent does not pass the evaluator on this cohort version.
  - **The set that passes:** the agent passes the evaluator on this cohort version with no change.
- For Loop F only: a system prompt that makes the agent pass on the set that fails.

The tool policy must not use `passthrough`, because the backend refuses live tools.

This runbook names no fixture. Ask the Teammate for the values if you do not have them.

Do not print the key. Do not write the key in a note, a story, a comment or a screenshot. Use the variable `$KITARU_API_KEY` in each command.

Set this helper in the shell. It sends a call to Kitaru with the key:

```bash
k() { curl -sS -H "Authorization: Bearer $KITARU_API_KEY" "$@"; }
```

The check starts approximately ten Runs on the Kitaru worker. Tell the Teammate before you start.

## Rules for all Loops

**Do one Loop at a time.** Do the three proofs (see "Proofs after an end state") immediately after each end state.

**Loop names.** Use `sc-345 <id> <date>`. `<id>` is the Loop letter (`A` to `F`), `row9` or `row10`. Example: `sc-345 A 20261117`. A name is unique for each agent. If you start a Loop again, add an attempt number: `sc-345 C 20261117 2`.

**Setup values.** Use these values for each `evalve_loop_start` call, unless the section of the Loop gives a different value:

| Parameter | Value |
|---|---|
| `cohort_version_id`, `evaluators`, `tool_policy` | From the fixture set that the section names |
| `allowed_changes` | `["system_prompt"]` |
| `allowed_models` | `[]` |
| `required_consecutive_greens` | `1` |
| `max_runs` | `3` |
| `budget_usd` | The Run estimate multiplied by 4 |

With these values, only the stop under test can end the Loop. The backend refuses a `max_runs` that is less than `required_consecutive_greens`.

**The Run estimate.** The backend estimates the cost of each Run before it starts the Run. Read the estimate of a fixture set in `experiments[0].runs[0].estimated_cost_usd`, in the result of a start or of a status call. For the first Loop on a fixture set, you do not know the estimate. Ask the Teammate for a budget that is sufficient for four Runs.

**If the start returns a refusal text:**

- "No cost is recorded for this cohort on this agent version. Provide control_estimate_usd." Add `control_estimate_usd` with an estimate from the Teammate. Then start again with the same name.
- "The next replay was estimated at $… and would exceed the budget." The budget is too small. The text gives the Run estimate. The backend kept a Loop with this name (see row 10). Start again with a larger budget and a new attempt number.

**The status call moves the Loop.** Each `evalve_loop_status` call from the Loop owner moves a `running` Loop one step. No tool waits. Call `evalve_loop_status` each minute until the result shows what the step expects.

**Tool results.** A tool returns the JSON of the backend route. For a refusal, the tool returns the refusal text only, with no status code. The text identifies the refusal. If a result starts with a line about versions, install the correct version of the Evalve MCP.

**"The backend cannot answer now (503)."** Wait one minute. Then call the same tool again.

**A different result.** If a result differs from the expected result, record the result that you got. Then continue with the next Loop. Do not try to correct the backend.

## The backend version

Read the health route:

```bash
curl -sS "$EVALVE_URL/health" | jq '{status, version, image}'
```

**Pass:** `status` is `ok`, and `version` is `0.2.0` or later. Record `version` and `image`.

If the version is earlier, stop. Tell the Teammate.

## Loop A: an Experiment that does not pass (row 8), then the run limit (row 1)

Use the set that fails. Use the setup values of the table, with `max_runs: 3`.

Each proposal in this Loop must be a system prompt that does not correct the failed cases. The two prompts must be different from each other.

1. Call `evalve_loop_start` with the name `sc-345 A <date>`. **Expected:** `status` is `running`, and `next.action` is `collect`. Record `loop_id`.
2. Record the Run estimate from `experiments[0].runs[0].estimated_cost_usd`. Loop B needs it.
3. Call `evalve_loop_status` each minute. **Expected:** `status` stays `running`, and `next.action` becomes `needs_change` with `needs.experiment: 1`. The Control Run was red.
4. Record `spend.accounted_usd`. This is the cost of the Control Run. Loop B needs it.
5. Call `evalve_loop_propose_experiment` with `experiment: 1`, a `change` sentence and the first `system_prompt`. **Expected:** the result has an `experiment_id`.
6. Call `evalve_loop_status` each minute. **Expected for row 8:** `status` is `running`, `next.action` is `needs_change`, `needs.experiment` is `2`, and `runs.used` is `2`. Record these values for row 8.
7. Call `evalve_loop_propose_experiment` with `experiment: 2`, a `change` sentence and the second `system_prompt`. **Expected:** the result has an `experiment_id`.
8. Call `evalve_loop_status` each minute. **Expected for row 1:** `status` is `completed`, `outcome` is `max_runs`, and `runs.used` is `3`. The reason text is "The total replay attempt cap was reached."
9. Do the three proofs.

If a proposed Run is green, the Loop ends with `completed` and `passed`. That Loop is a valid result for row 4. Do the three proofs on it, and record it for row 4. Then start Loop A again with a new attempt number.

If the Control Run is green, the fixture set does not fail. Tell the Teammate.

## Loop B: the budget stop (row 2)

Use the set that fails. Set `budget_usd` to the Run estimate from Loop A plus `0.01`.

The backend refuses a Run when the accounted cost plus the estimate of the Run is more than the budget. With this budget, the Control Run starts, and the first Run of Experiment 1 does not.

1. Call `evalve_loop_start` with the name `sc-345 B <date>`. **Expected:** `status` is `running`, and `next.action` is `collect`. Record `loop_id`.
2. Call `evalve_loop_status` each minute. **Expected:** `next.action` becomes `needs_change` with `needs.experiment: 1`.
3. Read `spend.accounted_usd`. If it is `0.01` or less, the proposal will start a Run. Go to "If Loop B takes a different path", item 2.
4. Call `evalve_loop_propose_experiment` with `experiment: 1`, a `change` sentence and a new `system_prompt`. **Expected:** the tool returns the text "This Experiment did not start because the Loop ended." Record the text.
5. Call `evalve_loop_status`. **Expected for row 2:** `status` is `completed`, and `outcome` is `budget_reached`. The reason text starts with "The next replay was estimated at" and ends with "would exceed the budget."
6. Make sure that Experiment 1 started no Kitaru run. **Expected:** `experiments[1].experiment_id` is `null`, and `experiments[1].runs[0].run_id` is `null`. `runs.used` is `2`, because the backend counts the Run that it did not start.
7. Do the three proofs. For the proof "No more Runs", use the Control Experiment, because Experiment 1 has no Kitaru experiment.

### If Loop B takes a different path

1. **The Loop ends before a proposal is possible.** The status shows `completed` and `budget_reached`, with the reason text "The replay budget was reached." The Control Run cost as much as the budget, or more. Record this Loop as an observation. Then start Loop B again with a new attempt number. Set `budget_usd` to `spend.accounted_usd` of this Loop plus `0.01`.
2. **The proposal starts a Run.** The result of the proposal has an `experiment_id`. The Control Run cost `0.01` or less. Call `evalve_loop_cancel` on this Loop. Then start Loop B again with a new attempt number. Set `budget_usd` to the Run estimate plus half of `spend.accounted_usd`. If `spend.accounted_usd` is `0`, this fixture set cannot give row 2. Tell the Teammate.

## Loop C: the Loop failure (row 3)

Use the set that fails. Use the setup values of the table. `max_runs: 3` makes sure that the run limit does not end the Loop first.

Two Runs in sequence that the backend cannot score end the Loop as `failed`. A Run that Kitaru cancelled cannot be scored. You cancel two Runs in Kitaru.

Cancel each Run immediately after it starts. A Run on a small cohort can end before the cancel arrives.

1. Call `evalve_loop_start` with the name `sc-345 C <date>`. **Expected:** `status` is `running`, and `next.action` is `collect`. Record `loop_id`.
2. Read the id of the Kitaru run in `experiments[0].runs[0].run_id`. Set it as `RUN_ID` in the shell.
3. Cancel the run in Kitaru. Do not call `evalve_loop_status` before this step.

   ```bash
   k -X POST "$KITARU_URL/api/v1/experiment-runs/$RUN_ID/cancel" | jq '{id, status}'
   ```

4. Read the run until its status is `canceled`. Kitaru writes this word with one "l". If the status is not yet `canceled`, read it again after some seconds.

   ```bash
   k "$KITARU_URL/api/v1/experiment-runs/$RUN_ID" | jq -r '.status'
   ```

5. Call `evalve_loop_status` one time. This call collects the cancelled Run and starts the retry Run. **Expected:** `status` is `running`, `next.action` is `collect`, and `runs.used` is `2`. `experiments[0].runs[0].result.outcome` is `incomplete`.
6. Read the id of the retry run in `experiments[0].runs[1].run_id`. Set it as `RUN_ID`.
7. Do steps 3 and 4 again for the retry run.
8. Call `evalve_loop_status`. **Expected for row 3:** `status` is `failed`, and `outcome` is `error`. The reason text is "Two Runs in a row could not be scored."
9. Do the three proofs.

If a Run ended before your cancel, the status of the Kitaru run is `completed`, and the Loop goes to `needs_change`. This Loop cannot give row 3. Call `evalve_loop_cancel` on it. Then start Loop C again with a new attempt number.

## Loop D: the Control Experiment passes (row 5)

Use the set that passes. Use the setup values of the table, with `required_consecutive_greens: 1`.

1. Call `evalve_loop_start` with the name `sc-345 D <date>`. **Expected:** `status` is `running`, and `next.action` is `collect`. Record `loop_id`.
2. Call `evalve_loop_status` each minute. **Expected for row 5:** `status` is `completed`, `outcome` is `already_passing`, and `runs.used` is `1`.
3. Do the three proofs.

If the next action becomes `needs_change`, the Control Run was red. The fixture set does not pass. Call `evalve_loop_cancel` on the Loop, and tell the Teammate.

## Loop E: the cancel (row 6)

Use the set that fails. Use the setup values of the table.

1. Call `evalve_loop_start` with the name `sc-345 E <date>`. **Expected:** `status` is `running`, and `next.action` is `collect`. Record `loop_id`.
2. Read the id of the Kitaru run in `experiments[0].runs[0].run_id`. Set it as `RUN_ID` in the shell.
3. Call `evalve_loop_cancel` immediately. Do not call `evalve_loop_status` before this step. **Expected for row 6:** `status` is `cancelled`, and `outcome` is `stopped_by_user`. The backend writes `cancelled` with two "l".
4. Read the Kitaru run. **Expected:** the status is `canceled`. If the status is not yet `canceled`, read it again after some seconds.

   ```bash
   k "$KITARU_URL/api/v1/experiment-runs/$RUN_ID" | jq -r '.status'
   ```

5. Do the three proofs.

If `evalve_loop_cancel` returns "The backend cannot answer now (503)", the backend waits for the start request to settle. This can continue for two minutes. Call `evalve_loop_cancel` again after one minute.

If the Kitaru run shows `completed`, the Run ended before the cancel. The Loop is `cancelled`, but it does not show that the backend cancels a Kitaru run. Record this Loop as an observation. Then start Loop E again with a new attempt number.

## Row 4: an Experiment passes

A Loop that ended with `completed` and `passed` gives this row. You must be the Loop owner, because two of the proofs need the key of the Loop owner. Use the first of these Loops that you have:

1. **A Loop A attempt that passed.** See the end of the Loop A section.
2. **The Loop of the demo story (sc-297).** Call `evalve_loop_list` with `status: completed`. The tool lists your Loops only. If the demo Loop is in the list with the outcome `passed`, record its `loop_id`. Then do the three proofs on it.
3. **Loop F.** Do the steps below.

### Loop F

Use the set that fails. Use the setup values of the table. You also need the system prompt that passes.

1. Call `evalve_loop_start` with the name `sc-345 F <date>`. **Expected:** `status` is `running`, and `next.action` is `collect`. Record `loop_id`.
2. Call `evalve_loop_status` each minute. **Expected:** `next.action` becomes `needs_change` with `needs.experiment: 1`.
3. Call `evalve_loop_propose_experiment` with `experiment: 1`, a `change` sentence and the `system_prompt` that passes. **Expected:** the result has an `experiment_id`.
4. Call `evalve_loop_status` each minute. **Expected for row 4:** `status` is `completed`, and `outcome` is `passed`. `winner.experiment` is `1`.
5. Do the three proofs.

If the Run of Experiment 1 is red, the next action is `needs_change` again. One more proposal is possible before the run limit. Ask the Teammate for a different system prompt.

## Row 9: live tools

The backend refuses a start that permits live tools. It refuses before it reads Kitaru, so it creates no Loop.

1. Call `evalve_loop_list`. Record the `loop_id` of the first item. It is your newest Loop.
2. Call `evalve_loop_start` with the name `sc-345 row9 <date>`, the set that fails, the setup values of the table, and `allow_live_tools: true`. **Expected for row 9:** the tool returns the text "Live tools are disabled for Loops in v1." The result has no `loop_id`. Record the text.
3. Call `evalve_loop_list` again. **Expected:** the first item has the same `loop_id` as in step 1. No Loop has the name `sc-345 row9 <date>`.

## Row 10: a budget below one Run estimate

1. Call `evalve_loop_start` with the name `sc-345 row10 <date>`, the set that fails, and the setup values of the table. Set `budget_usd` to half of the Run estimate from Loop A. **Expected for row 10:** the tool returns a text that starts with "The next replay was estimated at" and ends with "would exceed the budget." The result has no `loop_id`. Record the text.
2. Call `evalve_loop_list`. Look for a Loop with the name `sc-345 row10 <date>`. From the code, the backend keeps this Loop with `completed` and `budget_reached`, and with no Kitaru experiment. Record what the list shows, and the `loop_id` if there is one. This is an observation, not a different result.

Do not do the three proofs for row 10.

## Proofs after an end state

Do these three proofs for each Loop that gives one of rows 1 to 6. Do them only after the Loop is at an end state.

### 1. No more Runs

1. Call `evalve_loop_status`. Record `runs.used`.
2. Wait one minute. Call `evalve_loop_status` again. **Pass:** `runs.used` is the same.
3. Find the last Experiment of the Loop that has an `experiment_id`. Set that id as `EXPERIMENT_ID` in the shell. Count the Runs of that Experiment that have a `run_id`.
4. Read the runs of that experiment in Kitaru, one time:

   ```bash
   k -G "$KITARU_URL/api/v1/experiment-runs" \
     --data-urlencode "filter={\"field\":\"experiment_id\",\"op\":\"eq\",\"value\":\"$EXPERIMENT_ID\"}" \
     | jq '.items | length'
   ```

   **Pass:** the number is equal to the count from step 3.

### 2. The refusal

1. Call `evalve_loop_propose_experiment` on the Loop. Use the number of the last Experiment plus 1 as `experiment`. Send a `change` sentence and a `system_prompt` that the Loop did not use.
2. **Pass:** the tool returns the text "The Loop is not waiting for this Experiment." The result has no `experiment_id`. Record the text.

### 3. The Evalve MCP and the API agree

1. Call the API with the key of the Loop owner. Set the `loop_id` as `LOOP_ID` in the shell.

   ```bash
   curl -sS -H "Authorization: Bearer $KITARU_API_KEY" "$EVALVE_URL/v1/loops/$LOOP_ID" | jq '{status, outcome}'
   ```

2. **Pass:** `status` and `outcome` are the same as in the result of `evalve_loop_status`.

Do not make this call on a `running` Loop. A call from the Loop owner moves the Loop.

## Row 7: the idle limit

A Loop that waits for a change for more than 24 hours ends with `completed` and `stopped_idle`. Three automated tests show this. Do not change `waiting_since` in the database on AWS.

From a checkout of `evalve-backend` on `main`:

```bash
python3 -m venv .venv && .venv/bin/pip install .
.venv/bin/python -B -m unittest discover -s tests -v \
  -k test_a_status_call_after_24_hours_stops_the_loop \
  -k test_a_change_after_the_limit_is_refused_and_starts_no_run \
  -k test_late_proposal_expires_the_wait_without_starting_an_experiment
git rev-parse --short HEAD
```

**Pass:** the output shows `Ran 3 tests` and `OK`. Record the names of the three tests and the commit.

These tests need no database.

## Record the result

Write the results in one comment on the story sc-345. Do not write them on a PR.

Start the comment with these lines:

- the date of the check;
- `version` and `image` from the health route;
- the fixture ids of the two sets: the cohort version IDs and the evaluator references;
- for row 7: the names of the three tests and the commit on which they passed;
- each observation, for example the Loop that row 10 kept.

Then copy this table and complete it:

| Row | Method | Loop | Loop id | Status | Outcome | Refusal text | No more Runs | API agrees |
|---|---|---|---|---|---|---|---|---|
| 1 Run limit | AWS, Evalve MCP | A | | | | | | |
| 2 Budget stop | AWS, Evalve MCP | B | | | | | | |
| 3 Loop failure | AWS, Evalve MCP | C | | | | | | |
| 4 An Experiment passes | AWS, Evalve MCP | | | | | | | |
| 5 The Control Experiment passes | AWS, Evalve MCP | D | | | | | | |
| 6 Cancel | AWS, Evalve MCP | E | | | | | | |
| 7 Idle limit | Automated tests | none | | | | | | |
| 8 Experiment that does not pass | AWS, Evalve MCP | A | | | | | | |
| 9 Live tools | AWS, Evalve MCP | none | | | | | | |
| 10 Start refusal for the budget | AWS, Evalve MCP | none | | | | | | |

How to complete the table:

- **Rows 1 to 6:** the Loop id, the status and the outcome from `evalve_loop_status`. In "Refusal text", write the text from proof 2. In the last two columns, write pass or fail for proofs 1 and 3.
- **Row 2:** also write the text that the proposal returned in step 4 of Loop B.
- **Row 6:** also write the status of the Kitaru run.
- **Row 8:** the Loop id of Loop A, the status `running`, and the next action `needs_change` in the column "Outcome".
- **Rows 9 and 10:** the refusal text. For row 9, also write that `evalve_loop_list` showed no new Loop.

The comment must have no key, no token and no full request body. If a result differs from the expected result, add the reason text of the Loop and the tool result.

## Bug stories

Each result that differs from the expected result gets a story in Shortcut:

- the type is bug, in the epic 26;
- the labels are `ms:mcp-with-backend-on-aws`, `repo:backend` and `needs-triage`;
- the story has a "relates to" link to sc-345.

Do not correct the problem in this check. An observation gets no bug story, unless the Teammate decides that it is a bug.

## After the check

The Loops and their Kitaru experiments stay on the AWS server. Do not delete them. Each account on the server can see the Kitaru experiments.

Make sure that no Loop of this check is `running`. Call `evalve_loop_list` with `status: running`. Call `evalve_loop_cancel` on each Loop with a name that starts with `sc-345`.
