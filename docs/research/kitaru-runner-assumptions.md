# Does Kitaru 0.27.6 behave as the Round runner assumes?

Research for Shortcut story 310. The assumptions come from story 268 (Round runner).
Date: 2026-11-16. No live server was called. Every answer comes from the documents below.

## Sources

All Kitaru citations point at the public repository `zenml-io/kitaru`, tag `python/kitaru/v0.27.6`
(commit `96dfb8236f09720a3d31a9c3a67cf4ac7e19ca91`). Paths below are relative to
<https://github.com/zenml-io/kitaru/tree/96dfb8236f09720a3d31a9c3a67cf4ac7e19ca91>.

- **Source**: `src/kitaru/server/...` (the server), `src/kitaru/transport.py`, `src/kitaru/mcp/...`.
- **Docs**: `docs/book/...` (published at <https://docs.zenml.io/kitaru>).
- **OpenAPI**: `evalve-ui/openapi.json`, version `0.27.6`. It has the same content as `openapi/openapi.json` at the tag (compared as parsed JSON).
- **Deployment**: `evalve-deploy/infra/refresh.py:36-50` sets `KITARU_SERVER_AUTH_SCHEME=local` and does not set an idempotency retention value, so the AWS server uses the defaults.

"Verified" means the answer is read from source or docs at that tag. It does not mean the behaviour was seen on the AWS server.

## Summary

| # | Assumption in story 268 | Result |
|---|---|---|
| 1 | Idempotency keys expire after 15 minutes | **Verified.** Default 900 s, configurable. Deleted by a sweep, so the real lifetime is 900 s plus up to about one sweep interval (15 s). |
| 2 | The runner can find an evaluator or experiment by `name` | **Verified, with one correction.** `eq` is an exact match. Evaluator names are unique on the whole server. Experiment names are unique **per agent**, so the lookup must also filter on `agent_id`. A duplicate create returns 409. |
| 3 | `eq` on `experiment_run_id` works in the session list | **Verified.** It returns only the result sessions of the run's replays, not the baseline sessions. |
| 4 | `session.cost` can be null and leaves out judge costs | **Verified.** Null when any LLM call node has no cost, or the session has no nodes. Only the session's own nodes are summed. |
| 5 | Experiments belong to the backend's account | **Verified for the API and MCP.** Ownership is a label only. Every authenticated account can read and change the experiment. The bundled Kitaru UI source is not public; see the test below. |
| 6 | `GET /api/v1/accounts/me` | **Verified: it does not show the credential type.** No rate limit exists in the server or the docs. |

Changes this makes to story 268 are in [What changes for the runner](#what-changes-for-the-runner).

## 1. Idempotency keys

**Verified from source and docs.**

- Retention is `IDEMPOTENCY_KEY_RETENTION_SECONDS: int = 900` (`src/kitaru/server/config.py:74`). The environment variable is `KITARU_SERVER_IDEMPOTENCY_KEY_RETENTION_SECONDS` (`docs/book/deploy/docker.md:60`).
- Keys are deleted by the task sweeper. The cutoff is `now - retention`, measured from the row's `created` time (`src/kitaru/server/api/task_sweeper.py:128`, `.../db/repositories/idempotency_key_repository.py:140-161`). The sweep runs every `TASK_SWEEP_INTERVAL_SECONDS = 15` (`config.py:72`). So a key lives for at least 900 s and a little longer. The window starts at the first request, not at the last retry.
- Docs: "Stored keys expire after `KITARU_SERVER_IDEMPOTENCY_KEY_RETENTION_SECONDS` (15 minutes by default)" and "they protect against retries and double submits inside the retention window rather than acting as a permanent uniqueness constraint" (`docs/book/concepts/under-the-hood.md:20,28`).

Other rules that the runner depends on (`src/kitaru/server/adapters/rest/idempotency.py:135-223`, `.../rest/route.py:183-226`):

- **Scope**: the key is unique per account, across all endpoints (`UniqueConstraint("account_id", "key")`, `.../db/orm/idempotency_key.py:62-64`). The experiment-create key and the run-start key must be different strings. The story's keys (`evalve-{loop}-lap-{n}` and `evalve-{loop}-lap-{n}-run-{k}`) are different.
- **Fingerprint**: SHA-256 of method, path, query string and the **raw body bytes** (`idempotency.py:60-72`). The body must be byte-identical on a retry, not only equal JSON. A different body with the same key gives 422 (`IdempotencyKeyMismatch` is a `ValidationError`, `src/kitaru/server/domain/idempotency_key.py:43`).
- **Replay**: a matching retry returns the stored status and body with the header `Idempotent-Replayed: true` (`route.py:191-197`).
- **What is stored**: only 2xx, non-streaming responses (`route.py:203-216`). An error raised by the handler rolls the request back, key row included, so a retry runs again. Docs: "A failed request stores nothing" (`under-the-hood.md:20`).
- **409**: `"Idempotency-Key request is still in progress."` when the key row exists with no stored response (`idempotency.py:205-209`).
- **Limits**: at most 255 printable characters, else 400 (`idempotency.py:171-175`, `domain/idempotency_key.py:24`).
- The header is accepted on `POST /api/v1/experiments` and `POST /api/v1/experiments/{experiment_id}/runs` (OpenAPI, `Idempotency-Key` header parameter on both operations).

**Not verifiable without a live test**: that the AWS server uses the default. Test: `POST /api/v1/experiments/{id}/runs` twice with one key, 16 minutes apart. The second call returns a new run (a new `id` and `number`, no `Idempotent-Replayed` header) if retention is 900 s.

## 2. The `name` filter, and duplicate names

**Verified from source.**

**Matching.** `{"field":"name","op":"eq","value":"..."}` compiles to SQL `column == value` (`src/kitaru/server/adapters/db/filtering.py:135-150`). On the `VARCHAR` name columns this is an exact, whole-string match. It is not a prefix or substring match; those are the separate operators `startswith`, `endswith` and `contains`. `name` accepts `eq`, `ne`, `in`, `startswith`, `endswith`, `contains` on both resources (`STRING_OPS`, `src/kitaru/server/filtering.py:30-36`; `.../application/models/experiment.py:34`, `.../application/models/plugin.py:44,54,64`). A field or operator outside the allowlist gives 422 (`filtering.py:136-143`).

Case sensitivity follows the database collation. The columns are plain `String` with no case-insensitive type (`.../db/orm/plugin.py:82`, `.../db/orm/experiment.py:145`), so on PostgreSQL the match is case-sensitive.

**Evaluators.** The unique constraint is `("kind", "name")` on the plugin table (`.../db/orm/plugin.py:52,65`). An evaluator name is unique on the whole server, across accounts and agents, including agent-scoped evaluators. So `name eq` returns zero or one evaluator. A duplicate `POST /api/v1/evaluators` returns 409 (`DuplicatePluginName` is a `ConflictError`, `src/kitaru/server/domain/plugin.py:64`; OpenAPI: "409 when the name is already registered").

**Experiments.** The unique constraint is `("agent_id", "name")` (`.../db/orm/experiment.py:108-110,124-126`). Two agents can each have an experiment with the same name. So `name eq` alone can return more than one experiment. Filter on both:

```json
{"and":[{"field":"name","op":"eq","value":"my-loop-round-0"},
        {"field":"agent_id","op":"eq","value":"<agent uuid>"}]}
```

`agent_id` is filterable on experiments (`.../application/models/experiment.py:33`). A duplicate `POST /api/v1/experiments` for the same agent returns 409 with detail `Experiment name '<name>' is already registered` (`src/kitaru/server/domain/experiment.py:38-47`). The 409 body has only `detail`; it does not carry the existing experiment's id, so the runner must read it back with the filter above.

**Not verifiable without a live test**: the collation of the AWS database. Test: `GET /api/v1/evaluators?filter={"field":"name","op":"eq","value":"KITARU/COST"}` returns an empty page if the match is case-sensitive.

## 3. `eq` on `experiment_run_id` in the session list

**Verified from source.**

- `experiment_run_id` is a filterable field of `GET /api/v1/sessions`. It accepts only `eq` and `in` (`SCOPE_OPS`; `src/kitaru/server/application/models/session.py:58`, `src/kitaru/server/filtering.py:39-44`). `ne` gives 422; use `{"not": ...}` to negate.
- It is not a column of the session. It compiles to `EXISTS (replay WHERE replay.result_session_id = session.id AND replay.experiment_run_id = :value)` (`.../db/repositories/session_repository.py:69-75,140`; `.../db/filtering.py:113-130`). The source comment says: "Matched through the replay's result session, so a run's baseline sessions stay out of the result set."

Consequences:

- The list has one session per replay that already has a result session. A replay that has not started, or failed before it made a session, adds no row. The runner must compare the count with the cohort size (as `evalve-ui/public/skills/evalve-loop/0.1.0/scripts/loop.py:575-578` does) and must still read `GET /api/v1/replays` to map each result session to its `baseline_session_id`. The session row has no such field.
- The same filter (`eq`, `in`) exists on `GET /api/v1/replays` and `GET /api/v1/evaluations` (`.../application/models/replay.py:40`, `.../application/models/evaluation.py:46`).
- With this filter the runner can read all result sessions, with `cost`, in one paged call. `loop.py:548` reads each session one at a time.

**Not verifiable without a live test**: nothing in the logic. The story's demo check stays useful as a smoke test: after a completed run, `GET /api/v1/sessions?filter={"field":"experiment_run_id","op":"eq","value":"<run id>"}` returns exactly the `result_session_id` set from `GET /api/v1/replays` with the same filter.

## 4. `cost` in the session list

**Verified from source and OpenAPI.**

- **Present**: `SessionResponse.cost` is `string | null`, "Total cost" (`evalve-ui/openapi.json`, schema `SessionResponse`, the `cost` property at line 26205). The list returns `Page_SessionResponse_`, or `Page_SessionDetailResponse_` with `include_payloads=true`; both carry `cost`. The value is a decimal sent as a string. `cost` is also filterable and null-testable (`.../application/models/session.py:66`).
- **How it is computed**: the server recomputes it each time nodes are written: `SUM(session_node.cost)` over the session's nodes (`.../db/repositories/session_node_repository.py:57-103`, called from `.../application/services/session_node_service.py:195-196`).
- **When it is null**:
  1. Any `llm_call` node of the session has a null `cost`. Then the whole session cost is null, not a partial sum (`session_node_repository.py:66-73`).
  2. The session has no nodes yet (the column default is null, `.../db/orm/session.py:157`).
  
  A node cost is null when the adapter could not price the call. Examples: the Mastra adapter stores a cost only with a `costCalculator` (`docs/book/adapters/mastra.md:113`); other adapters estimate from the bundled `genai-prices` catalog (`docs/book/adapters/openai-agents.md:92`), which can miss a model.
- **While a session runs**, `cost` is the sum so far. Read it after the session is `completed`.
- **Judge costs are not included.** The sum covers only nodes of that session. An evaluation is a separate record, and `EvaluationResponse` has no cost field (`evalve-ui/openapi.json`, schema `EvaluationResponse`). No API field reports what an LLM judge spent. The docs treat it as a separate spend: "LLM judges spend judge tokens per session" (`docs/book/guides/llm-calls.md:62`).

**Not verifiable without a live test**: that no evaluator in use writes nodes into the session it scores. Test: note `cost` of a completed result session, run an LLM-judge evaluation on it, read `cost` again. It must not change.

## 5. Who sees an experiment that a service account owns

**Verified from source and docs for the API and the MCP. Not verifiable for the bundled UI.**

- **Docs**: "A Kitaru server is a **trusted-team deployment**: everyone authenticated can read and write everything, and ownership records who created a resource without gating access." The one exception is account administration (`docs/book/deploy/authentication.md:8`). Also `docs/book/concepts/under-the-hood.md:45`.
- **API**: under the `local` scheme the permission provider is `AdminFlagPermissionProvider`. It returns `ALL_IDS` for every list, and refuses only account create, update, deactivate and set-admin for non-admins (`src/kitaru/server/adapters/permissions/admin_flag.py:26-77`, wired at `.../rest/dependencies/services.py:159-161`). `owner_id` is set to the caller on create (`.../application/services/experiment_service.py:223,487`) and returned in `ExperimentResponse.owner_id`. It is not a filterable field on experiments, runs or sessions; only API keys and secrets are scoped to their owner. So every account, user or service account, admin or not, can list, read, patch, delete, run and cancel the backend's experiments.
- **Kitaru MCP**: it is a client of the same API. It uses `KITARU_API_KEY` or the stored login for the server URL (`src/kitaru/mcp/connection.py:40-53`, `docs/book/agent-native/setup.md:51`). No MCP code filters on owner. It sees what its account sees, which is everything.
- **Kitaru UI**: the UI is built in another repository and only bundled into the wheel (`FRONTEND-TESTING.md:14-31`); that repository is not public. The API gives the UI no owner filter for experiments, so it cannot hide them on the server side. A client-side "mine only" view cannot be excluded from these sources.

**Live test for the UI**: create an experiment with the service account's `KITKEY_` key, sign in to the Kitaru UI as a different, non-admin user, and open the experiment list for that agent.

A consequence for the design: "experiments belong to the backend's Kitaru account" gives a provenance label (`owner_id`) and nothing else. Any signed-in person can edit or delete a Loop's experiment, or start another run on it. The second case matters for run adoption; see below.

## 6. `GET /api/v1/accounts/me`

**Credential type: verified, it is not shown.**

The route returns the resolved account only (`src/kitaru/server/adapters/rest/routers/accounts.py:55-69`, `.../rest/mapping/accounts.py:27-47`). `AccountResponse` has `id`, `name`, `email`, `is_service_account`, `is_admin`, `active`, `metadata`, `created`, `updated` (`evalve-ui/openapi.json`, schema `AccountResponse`, line 16811). No field names the credential.

What a caller can and cannot tell:

- Kitaru has no "admin key". Admin is a flag on the account (`is_admin`), and a service account cannot be an admin (`docs/book/deploy/authentication.md:78`).
- An API key and a session token for the same account give the same answer. Any account, including a user, can create `KITKEY_` keys (`.../application/services/api_key_service.py:56-58`), and `POST /api/v1/login` can exchange an API key for a session token (`.../adapters/auth/auth_service.py:197`). So `is_service_account: false` does not mean "a person with a session token".
- Under the `local` scheme the server decides by prefix: a credential that starts with `KITKEY_` is an API key, anything else is decoded as a JWT (`auth_service.py:134-138`, `src/kitaru/api_models/v1/auth.py:23`). The Evalve backend holds the caller's `Authorization` header, so it can make the same prefix test itself. That is the only way to tell the type.
- Worker and task tokens get 403 on this route (`.../rest/dependencies/auth.py:353`, `authorize` accepts only an account principal).
- Under the `control_plane` scheme `KITKEY_` keys are rejected (`auth_service.py:357-369`). This does not apply to the AWS deployment, which uses `local`.

**Rate limit: verified, none is documented and none exists in the server.**

- The server source at the tag has no rate limiter, no 429 response and no `Retry-After` (search of `src/kitaru/server` for rate limit, throttle, 429). The OpenAPI document has no 429 response on any operation.
- The docs do not state a limit (search of `docs/book`).
- The only 429 reference is the client transport, which retries on 408, 429, 502, 503, 504 (`src/kitaru/transport.py:31`). This allows for a proxy in front of the server.
- One side effect: each API-key request may update the key's `last_used`, at most once per 60 s (`auth_service.py:69,436-441`).

- The deployment's proxy configuration in `evalve-deploy/infra/bootstrap.sh.tftpl` has no rate-limit directive (text search of the template).

**Not verifiable without a live test**: a limit applied outside these files, for example by AWS. Test: send about 50 requests in one second to `GET /api/v1/accounts/me` on the AWS server and look for 429.

## What changes for the runner

1. **Find an experiment by `name` and `agent_id`, not by `name` alone.** Experiment names are unique per agent only.
2. **"Adopts an existing run with the same `number`" needs care.** The server assigns `number` as the highest number in the experiment plus one (`.../application/services/experiment_service.py:485`). The client cannot choose it. `number` is not a filter field on `GET /api/v1/experiment-runs`; the runner must filter on `experiment_id` and compare `number` itself (`.../application/models/experiment_run.py:33-40`). "Run k of the lap is run number k" holds only if nothing else starts or deletes runs on that experiment, and question 5 shows that any account can do both. The runner should also compare `cohort_version_id`, `agent_version_id` and `created` before it adopts a run.
3. **Send byte-identical bodies on a retry.** The fingerprint covers the raw bytes and the query string. "Stable JSON" must mean fixed key order and fixed separators.
4. **The 15-minute window starts at the first attempt** and can end up to one sweep interval late. The story's rule (find or create by name, then adopt) is the right guard, because run start has no uniqueness rule after the key expires.
5. **A 409 on experiment create has two meanings**: a name conflict (read the experiment back) or `Idempotency-Key request is still in progress.` (wait and retry). Tell them apart by the `detail` text.
6. **Result sessions can come from one list call** with the `experiment_run_id` filter. The replay list is still needed for the baseline mapping and for replays with no result session.
7. **The `/accounts/me` answer cannot enforce "must be a `KITKEY_` key".** The prefix check on `EVALVE_KITARU_KEY` in settings is the real check. For callers, test the prefix of the forwarded credential if the type matters.
8. **No server rate limit**, so the 15-second poll is safe from that side. The sign-in check costs one Kitaru request per Evalve request.
9. The story's statement "`session.cost` can be null and leaves out judge costs" is correct as written.
