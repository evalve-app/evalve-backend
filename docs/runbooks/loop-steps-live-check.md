# Live check: one Loop on the AWS Kitaru server

This checks the last two acceptance criteria of sc-268, which need a real Kitaru server:

1. With the backend run locally against the AWS Kitaru server, a one-green Loop ends in `completed`, and the dashboard still shows it.
2. Kitaru behaves as the Loop steps assume. Part A lists each behaviour.

The tests cover everything else with a fake Kitaru. Use your own Kitaru key. The backend never stores it, and it must not go into notes, tickets or screenshots.

## What you need

- `KITARU_URL`: the AWS Kitaru server, for example `https://kitaru.example.com`, with no trailing slash.
- `KITARU_API_KEY`: your own Kitaru API key (`KITKEY_...`).
- A Kitaru worker connected to that server, so that runs actually execute.
- A cohort version whose agent already passes its evaluators. This makes the Loop end after one green Run. You need:
  - the cohort version ID (a UUID),
  - one evaluator reference (`name@version`) that passes on it,
  - your project's replay tool policy. It must not use `passthrough`, because v1 refuses live tools. The Evalve backend permits only the type `static`, and the type `history` with the scope `baseline`.
- Python 3.11 or newer, `curl`, `jq`, and a local PostgreSQL. With Homebrew: `brew services start postgresql@18`.

Set these in your shell:

```bash
export KITARU_URL=https://kitaru.example.com
export KITARU_API_KEY=KITKEY_...
export COHORT_VERSION_ID=...
export EVALUATOR=policy          # the evaluator's name
export EVALUATOR_VERSION=1
k() { curl -sS -H "Authorization: Bearer $KITARU_API_KEY" "$@"; }
```

## Part A: Kitaru behaviours, before the Loop

Each check prints something to compare. Write down pass or fail for each one.

**A1. The `name` filter on evaluators is an exact match.** It must print one evaluator name:

```bash
k -G "$KITARU_URL/api/v1/evaluators" --data-urlencode "filter={\"field\":\"name\",\"op\":\"eq\",\"value\":\"$EVALUATOR\"}" | jq '[.items[].name]'
```

The same command with the name in upper case must print `[]`. That confirms the match is case-sensitive.

**A2. An evaluator version is read at `/evaluators/{id}/versions/{number}`.** This path comes from the OpenAPI file, not from a live call. It must print the version number:

```bash
EVALUATOR_ID=$(k -G "$KITARU_URL/api/v1/evaluators" --data-urlencode "filter={\"field\":\"name\",\"op\":\"eq\",\"value\":\"$EVALUATOR\"}" | jq -r '.items[0].id')
k "$KITARU_URL/api/v1/evaluators/$EVALUATOR_ID/versions/$EVALUATOR_VERSION" | jq '.version'
```

**A3. Sessions can be filtered by cohort version.** The backend takes this field name from the dashboard's code. The two numbers must be equal:

```bash
k -G "$KITARU_URL/api/v1/sessions" --data-urlencode "filter={\"field\":\"cohort_version_id\",\"op\":\"eq\",\"value\":\"$COHORT_VERSION_ID\"}" --data-urlencode size=500 | jq '.items | length'
k "$KITARU_URL/api/v1/cohort-versions/$COHORT_VERSION_ID" | jq '.session_count'
```

A4 and A5 come after the Loop, because they need a finished run.

## Part B: run the backend locally against AWS

From the `evalve-backend` checkout, on the branch `jack/sc-268/loop-steps`:

```bash
python3 -m venv .venv && .venv/bin/pip install -e .
EVALVE_DB_HOST=localhost EVALVE_DB_SSL_MODE=disable \
EVALVE_DB_ADMIN_USER=postgres EVALVE_DB_ADMIN_PWD=postgres \
EVALVE_DB_USER=evalve EVALVE_DB_PWD=evalve-local-password \
EVALVE_DB_NAME=evalve_live_check \
.venv/bin/python -m evalve_backend setup-database
EVALVE_KITARU_URL=$KITARU_URL \
EVALVE_DB_HOST=localhost EVALVE_DB_USER=evalve EVALVE_DB_PWD=evalve-local-password EVALVE_DB_SSL_MODE=disable \
EVALVE_DB_NAME=evalve_live_check \
.venv/bin/python -m evalve_backend
```

Leave it running. In a second terminal, with the same exports, check that it answers and knows who you are:

```bash
curl -sS http://127.0.0.1:8100/evalve/health | jq
curl -sS -H "Authorization: Bearer $KITARU_API_KEY" http://127.0.0.1:8100/evalve/v1/me | jq
```

## Part C: one Loop

**C1. Start the Loop.** `required_consecutive_greens: 1` makes one green Run enough. Put your project's tool policy in `tool_policy`:

```bash
E=http://127.0.0.1:8100/evalve/v1/loops
LOOP=$(curl -sS -X POST "$E" -H "Authorization: Bearer $KITARU_API_KEY" -H "Idempotency-Key: live-check-$(date +%s)" \
  -H "Content-Type: application/json" -d "{
    \"name\": \"sc-268 live check $(date +%Y%m%d-%H%M)\",
    \"cohort_version_id\": \"$COHORT_VERSION_ID\",
    \"evaluators\": [{\"ref\": \"$EVALUATOR@$EVALUATOR_VERSION\"}],
    \"tool_policy\": {\"default\": {\"type\": \"history\", \"scope\": \"baseline\", \"on_miss\": \"fail\"}},
    \"allowed_changes\": [\"system_prompt\"], \"allowed_models\": [],
    \"required_consecutive_greens\": 1, \"max_runs\": 3, \"budget_usd\": \"1.00\"
  }")
echo "$LOOP" | jq '{loop_id, status, next: .next.action, detail}'
LOOP_ID=$(echo "$LOOP" | jq -r .loop_id)
```

You should see `status: running` and `next: collect`. A `422` explains what to fix, for example "Provide control_estimate_usd" when the cohort has no recorded cost. Add `"control_estimate_usd": "0.50"` in that case.

**C2. Poll until the Loop ends.** Each status call moves the Loop one step:

```bash
while :; do
  S=$(curl -sS -H "Authorization: Bearer $KITARU_API_KEY" "$E/$LOOP_ID")
  echo "$S" | jq -c '{status, outcome, next: .next.action, reason}'
  [ "$(echo "$S" | jq -r .status)" != running ] && break
  sleep 60
done
```

**Pass:** the last line shows `"status":"completed","outcome":"already_passing"`.

If the Run comes back red or incomplete instead, the Loop asks for a change. That still exercises the steps, but it isn't the one-green check. Pick a cohort and evaluator that pass, then start again.

**C3. The dashboard still shows it.** Open the Evalve dashboard on the AWS deployment and find the agent's experiment named `evalve-<loop_id>-exp-0`. **Pass:** the dashboard lists the Loop with its settings and a finished result.

You can also look at the experiment's description in Kitaru. It must end with an `Evalve result:` line whose `status` is `stop_success`:

```bash
EXP_ID=$(curl -sS -H "Authorization: Bearer $KITARU_API_KEY" "$E/$LOOP_ID" | jq -r '.experiments[0].experiment_id')
k "$KITARU_URL/api/v1/experiments/$EXP_ID" | jq -r '.description'
```

## Part A, continued: checks on the finished run

```bash
RUN_ID=$(curl -sS -H "Authorization: Bearer $KITARU_API_KEY" "$E/$LOOP_ID" | jq -r '.experiments[0].runs[-1].run_id')
RUN_FILTER="{\"field\":\"experiment_run_id\",\"op\":\"eq\",\"value\":\"$RUN_ID\"}"
```

**A4. `eq` on `experiment_run_id` returns exactly the run's result sessions.** The two lists must be equal:

```bash
k -G "$KITARU_URL/api/v1/sessions" --data-urlencode "filter=$RUN_FILTER" --data-urlencode size=500 | jq '[.items[].id] | sort'
k -G "$KITARU_URL/api/v1/replays" --data-urlencode "filter=$RUN_FILTER" --data-urlencode size=500 | jq '[.items[].result_session_id | select(.)] | sort'
```

**A5. `cost` is in the session list.** Every session must show a `cost`, as a string or `null`:

```bash
k -G "$KITARU_URL/api/v1/sessions" --data-urlencode "filter=$RUN_FILTER" | jq '[.items[] | {id, cost}]'
```

## Record the result

Post these on the PR, not in Shortcut:

- the Loop ID, and the final `status` and `outcome`;
- pass or fail for C3 and for A1 to A5.

Leave out keys, tokens and full request bodies. If a check fails, add the command and the `detail` that Kitaru answered with.

## Clean up

- Stop the backend with Ctrl+C.
- To drop the local database: `dropdb evalve_live_check`.
- The Kitaru experiment stays on the AWS server, where every account can see it. Delete it in Kitaru if your team prefers.
