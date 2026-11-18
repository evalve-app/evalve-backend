# End state check: each end state of a Loop, on the backend on AWS

This runbook gives the steps for rows 1 to 6 and 8 to 10 of the story [sc-345](https://app.shortcut.com/team-4-1/story/345). The reader is the coding agent of a Teammate. The coding agent calls the `evalve_*` tools of the Evalve MCP, which is connected to the Evalve backend on AWS.

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

Do Loops A, B and C first. They show rows 1, 2, 3 and 8, which are the most important rows.

## What you need

- The Evalve MCP, connected to the Evalve backend on AWS. The [README](../../README.md) gives the setup steps. Install the same version as the Evalve backend.
- `KITARU_API_KEY`: the Teammate's own Kitaru API key, in the environment. All Loops of this check must have the same Loop owner.
- `KITARU_URL`: the Kitaru server on AWS, with no slash at the end.
- `EVALVE_URL`: the Evalve backend, for example `https://<host>/evalve`. It is the `--server` value of the Evalve MCP.
- A Kitaru worker that is connected to the Kitaru server, so that Runs execute.
- `curl` and `jq`.
- Two fixture sets from the Teammate. A fixture set is a cohort version ID, one evaluator reference (`name@version`) and a replay tool policy.
  - **The set that fails:** the agent does not pass the evaluator on this cohort version.
  - **The set that passes:** the agent passes the evaluator on this cohort version with no change.
- For Loop F only: a system prompt that makes the agent pass on the set that fails.

The tool policy must not use `passthrough`, because the Evalve backend refuses live tools.

This runbook names no fixture. Ask the Teammate for the values if you do not have them.

Do not print the key. Do not write the key in a note, a story, a comment or a screenshot. Use the variable `$KITARU_API_KEY` in each command. Do not use `curl -v` or `set -x`, because they print the key.

Set this helper in the shell. It sends a call with the key:

```bash
k() { curl -sS -H "Authorization: Bearer $KITARU_API_KEY" "$@"; }
```

The check starts approximately ten Runs on the Kitaru worker. Tell the Teammate before you start.

## Rules for all Loops

**Terms.** The Control Run is the first Run of the Control Experiment. A retry Run is a second Run that the Evalve backend starts when it cannot score a Run. A Kitaru run is the record in Kitaru of one Run. A Kitaru experiment is the record in Kitaru of one Experiment.

**Do one Loop at a time.** Do the three proofs (see "Proofs after an end state") immediately after each end state.

**Loop names.** Use `sc-345 <id> <date>`. `<id>` is the Loop letter (`A` to `F`), `row9` or `row10`. Example: `sc-345 A 20261117`. If you start a Loop again, add a sequence number: `sc-345 C 20261117 2`. Two `running` Loops on one agent cannot have the same name.

**Setup values.** Use these values for each `evalve_loop_start` call, unless the section of the Loop gives a different value:

| Parameter | Value |
|---|---|
| `cohort_version_id`, `evaluators`, `tool_policy` | From the fixture set that the section names |
| `allowed_changes` | `["system_prompt"]` |
| `allowed_models` | `[]` |
| `required_consecutive_greens` | `1` |
| `max_runs` | `3` |
| `budget_usd` | The Run estimate multiplied by 4 |

With these values, the stop under test ends the Loop, unless a Run costs much more than its Run estimate. The Evalve backend refuses a `max_runs` that is less than `required_consecutive_greens`.

**The Run estimate.** The Evalve backend estimates the cost of each Run before it starts the Run. Read the Run estimate of a fixture set in `experiments[0].runs[0].estimated_cost_usd`, in the result of a start or of a status call. For the first Loop on a fixture set, you do not know the Run estimate. Ask the Teammate for a budget that is sufficient for four Runs.

**If the start returns a refusal text:**

- "No cost is recorded for this cohort on this agent version. Provide control_estimate_usd." Add `control_estimate_usd` with an estimate from the Teammate. Then start again.
- "The next replay was estimated at $… and would exceed the budget." The budget is too small. The text gives the Run estimate. Start again with a larger budget and a sequence number. The Evalve backend keeps the Loop that it refused (see row 10).

**If a Loop other than Loop B ends with `budget_reached`.** The Runs cost more than the Run estimate. Record the Loop as an observation. Start the Loop again with a larger budget and a sequence number.

**The status call moves the Loop.** Each `evalve_loop_status` call from the Loop owner moves a `running` Loop one step forward. No tool waits. Call `evalve_loop_status` each minute until the result shows the expected values.

**Tool results.** A tool returns the JSON of the backend route. For a refusal, the tool returns the refusal text only, with no status code. The text identifies the refusal. If a result starts with a line about versions, install the correct version of the Evalve MCP.

**"The backend cannot answer now (503)."** Wait one minute. Then do one of these:

- For `evalve_loop_start`: call `evalve_loop_list` first. If a Loop with your name is in the list, the start was successful. Use the `loop_id` of that Loop. If not, call `evalve_loop_start` again.
- For each other tool: call the same tool again.

**A different result.** If a result differs from the expected result, record the result that you got. Then continue with the next Loop. Do not try to correct the Evalve backend.

## The backend version

Read the health route:

```bash
curl -sS "$EVALVE_URL/health" | jq '{status, version, image}'
```

**Expected:** `status` is `ok`, and `version` is `0.2.0` or later. Record `version` and `image`.

If the version is earlier, stop. Tell the Teammate.

## Loop A: an Experiment that does not pass (row 8), then the run limit (row 1)

Use the set that fails. Use the setup values of the table, with `max_runs: 3`.

Each proposal in this Loop must be a system prompt that does not correct the failed cases. The two prompts must be different from each other.

1. Call `evalve_loop_start` with the name `sc-345 A <date>`. **Expected:** `status` is `running`, and `next.action` is `collect`. Record `loop_id`.
2. Record the Run estimate from `experiments[0].runs[0].estimated_cost_usd`. Loop B needs it.
3. Call `evalve_loop_status` each minute. **Expected:** `status` stays `running`, and `next.action` becomes `needs_change` with `needs.experiment: 1`. The Control Run was red.
4. Call `evalve_loop_propose_experiment` with `experiment: 1`, a `change` sentence and the first `system_prompt`. **Expected:** the result has an `experiment_id`.
5. Call `evalve_loop_status` each minute. **Expected for row 8:** `status` is `running`, `next.action` is `needs_change`, `needs.experiment` is `2`, and `runs.used` is `2`. Record these values for row 8.
6. Call `evalve_loop_propose_experiment` with `experiment: 2`, a `change` sentence and the second `system_prompt`. **Expected:** the result has an `experiment_id`.
7. Call `evalve_loop_status` each minute. **Expected for row 1:** `status` is `completed`, `outcome` is `max_runs`, and `runs.used` is `3`. The reason text is "The total replay attempt cap was reached."
8. Do the three proofs.

If a proposed Run is green, the Loop ends with `completed` and `passed`. That Loop shows row 4, if you have no demo Loop. Do the three proofs on it. Record it for row 4. Then start Loop A again with a sequence number.

If the Control Run is green, the fixture set does not fail. Tell the Teammate.

## Loop B: the budget stop (row 2)

Use the set that fails. Set `budget_usd` to the Run estimate from Loop A plus `0.01`.

The Evalve backend refuses a Run when the accounted cost plus the Run estimate is more than the budget. With this budget, the Control Run starts. The first Run of Experiment 1 does not start, if the Control Run cost more than `0.01`.

1. Call `evalve_loop_start` with the name `sc-345 B <date>`. **Expected:** `status` is `running`, and `next.action` is `collect`. Record `loop_id`.
2. Call `evalve_loop_status` each minute. **Expected:** `next.action` becomes `needs_change` with `needs.experiment: 1`.
3. Read `spend.accounted_usd`. This is the accounted cost of the Control Run. If it is `0.01` or less, the proposal of step 4 needs one more parameter. Add `estimate_usd` with the value of `budget_usd` plus `0.01`. Record that you added it.
4. Call `evalve_loop_propose_experiment` with `experiment: 1`, a `change` sentence and a new `system_prompt`. **Expected:** the tool returns the text "This Experiment did not start because the Loop ended." Record the text.
5. Call `evalve_loop_status`. **Expected for row 2:** `status` is `completed`, and `outcome` is `budget_reached`. The reason text starts with "The next replay was estimated at" and ends with "would exceed the budget."
6. Make sure that Experiment 1 started no Kitaru run. **Expected:** `experiments[1].experiment_id` is `null`, and `experiments[1].runs[0].run_id` is `null`. `runs.used` is `2`, because the Evalve backend counts the Run that it did not start.
7. Do the three proofs. For the proof "No more Runs", use the Control Experiment, because Experiment 1 has no Kitaru experiment.

### If Loop B takes a different path

**The Loop ends before a proposal is possible.** The status shows `completed` and `budget_reached`, with the reason text "The replay budget was reached." The Control Run cost as much as the budget, or more.

1. Record this Loop as an observation.
2. Start Loop B again with a sequence number. Set `budget_usd` to `spend.accounted_usd` of this Loop plus `0.01`.
3. If the same path occurs again, tell the Teammate.

**The proposal starts a Run.** The result of the proposal has an `experiment_id`.

1. Call `evalve_loop_cancel` on this Loop.
2. Start Loop B again with a sequence number.
3. In the proposal, add `estimate_usd` with the value of `budget_usd` plus `0.01`. Record that you added it.

## Loop C: the Loop failure (row 3)

Use the set that fails. Use the setup values of the table. `max_runs: 3` makes sure that the run limit does not end the Loop first.

Two Runs in sequence that the Evalve backend cannot score end the Loop as `failed`. The Evalve backend cannot score a Run that Kitaru cancelled. You cancel two Kitaru runs.

Cancel each Kitaru run immediately after it starts. A Run on a small cohort can end before the cancel arrives.

1. Call `evalve_loop_start` with the name `sc-345 C <date>`. **Expected:** `status` is `running`, and `next.action` is `collect`. Record `loop_id`.
2. Read the ID of the Kitaru run in `experiments[0].runs[0].run_id`. Set it as `RUN_ID` in the shell.
3. Cancel the Kitaru run. Do not call `evalve_loop_status` before this step. The command prints the status code. A `409` shows that the Kitaru run ended before the cancel.

   ```bash
   k -X POST -o /dev/null -w '%{http_code}\n' "$KITARU_URL/api/v1/experiment-runs/$RUN_ID/cancel"
   ```

4. Read the Kitaru run until its status is `canceled`. Kitaru writes this word with one "l". If the status is not yet `canceled`, read it again after 5 seconds.

   ```bash
   k "$KITARU_URL/api/v1/experiment-runs/$RUN_ID" | jq -r '.status'
   ```

5. Call `evalve_loop_status` one time. This call collects the cancelled Run and starts the retry Run. **Expected:** `status` is `running`, `next.action` is `collect`, and `runs.used` is `2`. `experiments[0].runs[0].result.outcome` is `incomplete`.
6. Read the ID of the Kitaru run of the retry Run in `experiments[0].runs[1].run_id`. Set it as `RUN_ID`.
7. Do steps 3 and 4 again for this Kitaru run.
8. Call `evalve_loop_status`. **Expected for row 3:** `status` is `failed`, and `outcome` is `error`. The reason text is "Two Runs in a row could not be scored."
9. Do the three proofs.

If a Run ended before your cancel, the status of the Kitaru run is `completed`. The Loop cannot show row 3. Call `evalve_loop_cancel` on it. Then start Loop C again with a sequence number.

## Loop D: the Control Experiment passes (row 5)

Use the set that passes. Use the setup values of the table, with `required_consecutive_greens: 1`.

1. Call `evalve_loop_start` with the name `sc-345 D <date>`. **Expected:** `status` is `running`, and `next.action` is `collect`. Record `loop_id`.
2. Call `evalve_loop_status` each minute. **Expected for row 5:** `status` is `completed`, `outcome` is `already_passing`, and `runs.used` is `1`.
3. Do the three proofs.

If the next action becomes `needs_change`, the Control Run was red. The fixture set does not pass. Call `evalve_loop_cancel` on the Loop. Then tell the Teammate.

## Loop E: the cancel (row 6)

Use the set that fails. Use the setup values of the table.

1. Call `evalve_loop_start` with the name `sc-345 E <date>`. **Expected:** `status` is `running`, and `next.action` is `collect`. Record `loop_id`.
2. Read the ID of the Kitaru run in `experiments[0].runs[0].run_id`. Set it as `RUN_ID` in the shell.
3. Call `evalve_loop_cancel` immediately. Do not call `evalve_loop_status` before this step. **Expected for row 6:** `status` is `cancelled`, and `outcome` is `stopped_by_user`. The Evalve backend writes `cancelled` with two "l".
4. Read the Kitaru run. **Expected:** the status is `canceled`. If the status is not yet `canceled`, read it again after 5 seconds.

   ```bash
   k "$KITARU_URL/api/v1/experiment-runs/$RUN_ID" | jq -r '.status'
   ```

5. Do the three proofs.

If the Kitaru run shows `completed`, the Run ended before the cancel. The Loop is `cancelled`, but it does not show that the Evalve backend cancels a Kitaru run. Record this Loop as an observation. Then start Loop E again with a sequence number.

## Row 4: an Experiment passes

A Loop that ended with `completed` and `passed` shows this row. You must be the Loop owner, because the proof "The refusal" needs the key of the Loop owner. Use the first of these Loops that you have:

1. **The Loop of the demo story (sc-297).** Ask the Teammate for its name. Call `evalve_loop_list` with `status: completed`. The tool lists your Loops only. If the demo Loop is in the list with the outcome `passed`, record its `loop_id`. Then do the three proofs on it.
2. **A Loop A that ended with `passed`.** See the end of the Loop A section.
3. **Loop F.** See the next section.

## Loop F: an Experiment passes (row 4)

Do this Loop only if you have no other Loop for row 4.

Use the set that fails. Use the setup values of the table. You also need the system prompt that passes.

1. Call `evalve_loop_start` with the name `sc-345 F <date>`. **Expected:** `status` is `running`, and `next.action` is `collect`. Record `loop_id`.
2. Call `evalve_loop_status` each minute. **Expected:** `next.action` becomes `needs_change` with `needs.experiment: 1`.
3. Call `evalve_loop_propose_experiment` with `experiment: 1`, a `change` sentence and the `system_prompt` that passes. **Expected:** the result has an `experiment_id`.
4. Call `evalve_loop_status` each minute. **Expected for row 4:** `status` is `completed`, and `outcome` is `passed`. `winner.experiment` is `1`.
5. Do the three proofs.

If the Run of Experiment 1 is red, the next action is `needs_change` again. One more proposal is possible before the run limit. Ask the Teammate for a different system prompt.

## Row 9: live tools

The Evalve backend refuses a start that permits live tools. It refuses before the preflight, so it creates no Loop.

1. Call `evalve_loop_list`. Record the `loop_id` of the first item. It is your newest Loop.
2. Call `evalve_loop_start` with the name `sc-345 row9 <date>` and `allow_live_tools: true`. Use the set that fails and the setup values of the table. **Expected for row 9:** the tool returns the text "Live tools are disabled for Loops in v1." The result has no `loop_id`. Record the text.
3. Call `evalve_loop_list` again. **Expected:** the first item has the same `loop_id` as in step 1. No Loop has the name `sc-345 row9 <date>`.

## Row 10: a budget below one Run estimate

1. Call `evalve_loop_start` with the name `sc-345 row10 <date>`. Use the set that fails and the setup values of the table. Set `budget_usd` to half of the Run estimate from Loop A. **Expected for row 10:** the tool returns a text that starts with "The next replay was estimated at" and ends with "would exceed the budget." The result has no `loop_id`. Record the text.
2. Call `evalve_loop_list`. Find a Loop with the name `sc-345 row10 <date>`. From the code, the Evalve backend keeps this Loop with `completed` and `budget_reached`, and with no Kitaru experiment. Record the values that the list shows, and the `loop_id` if there is one. This is an observation, not a different result.

Do not do the three proofs for row 10.

## Proofs after an end state

Do these three proofs for each Loop that shows one of rows 1 to 6. Do them only after the Loop is at an end state.

### 1. No more Runs

1. Call `evalve_loop_status`. Record `runs.used`.
2. Wait one minute. Call `evalve_loop_status` again. **Expected:** `runs.used` is the same.
3. Find the last Experiment of the Loop that has an `experiment_id`. Set that ID as `EXPERIMENT_ID` in the shell. Count the Runs of that Experiment that have a `run_id`.
4. Read the Kitaru runs of that Kitaru experiment, one time:

   ```bash
   k -G "$KITARU_URL/api/v1/experiment-runs" \
     --data-urlencode "filter={\"field\":\"experiment_id\",\"op\":\"eq\",\"value\":\"$EXPERIMENT_ID\"}" \
     | jq '.items | length'
   ```

   **Expected:** the number is equal to the count from step 3.

### 2. The refusal

1. Call `evalve_loop_propose_experiment` on the Loop. Use the number of the last Experiment plus 1 as `experiment`. Send a `change` sentence and a `system_prompt` that the Loop did not use.
2. **Expected:** the tool returns the text "The Loop is not waiting for this Experiment." The result has no `experiment_id`. Record the text.

### 3. The Evalve MCP and the API agree

1. Set the `loop_id` as `LOOP_ID` in the shell. Then call the API with the key of the Loop owner:

   ```bash
   k "$EVALVE_URL/v1/loops/$LOOP_ID" | jq '{status, outcome}'
   ```

2. **Expected:** `status` and `outcome` are the same as in the result of `evalve_loop_status`.

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

**Expected:** the output shows `Ran 3 tests` and `OK`. Record the names of the three tests and the commit.

These tests need no database.

## Record the result

Write the results in one comment on the story sc-345. Do not write them on a PR.

Start the comment with these lines:

- The date of the check.
- `version` and `image` from the health route.
- The fixture IDs of the two sets: the cohort version IDs and the evaluator references.
- For row 7: the names of the three tests and the commit on which they passed.
- Each observation, for example the Loop that row 10 kept.

Copy this table into the comment. Then complete it.

| Row | Method | Loop | Loop ID | Status | Outcome or next action | Refusal text | No more Runs | API agrees |
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

- **Rows 1 to 6:** the Loop ID, the status and the outcome from `evalve_loop_status`. In "Refusal text", write the text from proof 2. In the last two columns, write pass or fail for proofs 1 and 3.
- **Row 2:** also write the text that the proposal returned in step 4 of Loop B.
- **Row 6:** also write the status of the Kitaru run.
- **Row 8:** the Loop ID of Loop A, the status `running` and the next action `needs_change`.
- **Rows 9 and 10:** the refusal text. For row 9, also write that `evalve_loop_list` showed no new Loop.

The comment must have no key, no token and no full request body. If a result differs from the expected result, add the reason text of the Loop and the tool result.

## Bug stories

Each result that differs from the expected result gets a story in Shortcut:

- The type is bug, in the epic 26.
- The labels are `ms:mcp-with-backend-on-aws`, `repo:backend` and `needs-triage`.
- The story has a "relates to" link to sc-345.

Do not correct the problem in this check. An observation gets no bug story, unless the Teammate decides that it is a bug.

## After the check

The Loops and their Kitaru experiments stay on the Kitaru server and in the Evalve backend. Do not delete them.

Make sure that no Loop of this check is `running`. Call `evalve_loop_list` with `status: running`. Call `evalve_loop_cancel` on each Loop with a name that starts with `sc-345`.
