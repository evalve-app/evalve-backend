# evalve-backend

The Evalve backend stores loop data. It runs on the same server as Kitaru and keeps its tables in its own database on the same PostgreSQL instance.

The backend applies pending schema migrations before it starts serving requests. Its Loop store preserves committed Loops, Experiments, and Runs across backend restarts.

## Routes

Every route sits under `/evalve`, so one hostname can serve Kitaru and Evalve.

| Route | Sign-in | What it does |
| --- | --- | --- |
| `GET /evalve/health` | None | `200` with `"database": "ok"` when the backend can query its database. `503` when it can't. Both include `version` and `image`, so a deployment can confirm which build is serving. |
| `GET /evalve/v1/me` | Kitaru credential or dashboard session | `200 {"id": "...", "name": "..."}` for the Kitaru account behind the credential. |

The [Loop API contract](openapi/evalve-backend.json) covers start, list, status, Run evidence, Experiment proposals, cancellation, and the final report. Signed-in accounts can read all Loops. The list is newest first and supports an optional `owner_account_id` filter. Only the account that started a Loop can propose, cancel, or advance it. A status read by another account returns stored results.

A Loop moves forward only when its owner calls: a start, a status call, a change, or a cancel. Each call takes the Loop's lock, does at most one step in Kitaru with the caller's own key, and returns. The backend has no Kitaru key and no background task, and it never stores or logs the caller's key. When Kitaru can't answer, the call returns `503` and the next call tries again. When another call holds the Loop's lock, a status call returns the stored state.

## Evalve MCP

`evalve-mcp` is a second package in this repository, in [`evalve-mcp/`](evalve-mcp). It is a local MCP server through which a Teammate's coding agent runs Loops. The coding agent starts it as a subprocess on the Teammate's machine. It calls this backend over HTTPS with the Teammate's own Kitaru key. It has no Loop rules, it does not import the backend code, and it never runs on AWS.

### Install

Run these steps in the agent repository.

1. Read the backend version. The route needs no credential.

   ```bash
   curl https://<host>/evalve/health
   ```

2. Add the package from the Git tag of that `version`. The tag and the `uv.lock` of the agent repository pin the version. The package is not on PyPI.

   ```bash
   uv add "evalve-mcp @ git+https://github.com/evalve-app/evalve-backend@v<version>#subdirectory=evalve-mcp"
   ```

3. Write the Claude Code config.

   ```bash
   uv run evalve-mcp setup --server https://<host>/evalve
   ```

   The command writes this entry in the `.mcp.json` of the current directory. It creates the file when the file is missing, and it changes only the `evalve` entry. When the file is not valid JSON, the command stops and changes nothing.

   ```json
   {
     "mcpServers": {
       "evalve": {
         "command": "uv",
         "args": ["run", "evalve-mcp", "--server", "https://<host>/evalve"]
       }
     }
   }
   ```

   The entry holds no key, so the file is safe to commit.

4. Set `KITARU_API_KEY` in the environment in which Claude Code starts. This is the same variable that Kitaru uses. The server sends the key to the backend as a Bearer token and never prints or logs it. With no key, the server starts, and each tool returns a message that says to set the variable.

5. Start Claude Code. The `evalve_*` tools show next to the Kitaru tools.

The server URL must use HTTPS unless the host is localhost. When the server URL changes, run the setup command again with the new URL.

### Tools

| Tool | Route | What it does |
| --- | --- | --- |
| `evalve_loop_start` | `POST /v1/loops` | Starts a Loop and returns it at once. |
| `evalve_loop_status` | `GET /v1/loops/{loop_id}` | The status, the outcome, the next action and each Experiment. For the owner, it also moves the Loop forward. |
| `evalve_loop_list` | `GET /v1/loops` | The Loops of the caller, with an optional status filter and a cursor. It reads the caller's account ID from `GET /v1/me` one time. |
| `evalve_loop_propose_experiment` | `POST /v1/loops/{loop_id}/experiments` | Sends one change (`system_prompt` or `model`) for the next Experiment. |
| `evalve_loop_cancel` | `POST /v1/loops/{loop_id}/cancel` | Cancels a Loop. |
| `evalve_loop_run_evidence` | `GET /v1/loops/{loop_id}/experiments/{experiment}/runs/{run}` | The failed cases and the evaluator explanations of one run. |

Each tool takes the fields of the API body as typed parameters and returns the backend JSON with no change, after the version line when there is one. No tool waits for a Loop to end: the coding agent starts a Loop, then calls `evalve_loop_status` every minute or so.

- **Repeats.** On a `503`, a connection error or a time limit, the server repeats the call after 2 seconds and then after 5 seconds. Then the tool returns a message that says the Loop is not lost. The time limit is 60 seconds for a start and a proposal, and 15 seconds for each other call.
- **One Loop for each start call.** The repeats of one `evalve_loop_start` call send the same `Idempotency-Key`. A second tool call sends a new key, and the backend then answers `409` because the name is in use.
- **Versions.** At startup the server reads `version` from the health route. When the major or minor number differs from its own, each tool result starts with one line that names the two versions and the install command. The server continues to work.

### Tests

```bash
.venv/bin/pip install -e ./evalve-mcp
.venv/bin/python -B -m unittest discover -s evalve-mcp/tests
```

The tests use a fake backend. They also check each path, method and field that the tools use against [`openapi/evalve-backend.json`](openapi/evalve-backend.json).

## Sign-in

Evalve has no accounts of its own. A caller sends the same credential it would send to Kitaru, in one of two ways.

**A key or a token.** The Evalve MCP and scripts use this:

```
Authorization: Bearer <Kitaru API key or token>
```

**The dashboard session.** A browser that signed in to Kitaru through the dashboard sends Kitaru's session cookie and the CSRF token of the login answer:

```
Cookie: evalve_session=<session token>
X-CSRF-Token: <csrf_token>
```

The backend passes the credential to `GET /api/v1/accounts/me` on the Kitaru server named in `EVALVE_KITARU_URL` and accepts the request if Kitaru does. It uses the same credential for each Kitaru call that it makes for the caller.

- **Only the configured Kitaru server is asked.** Nothing in a request can change which server that is.
- **The credential is never stored or logged.** It is passed to Kitaru and dropped. This includes the cookie and the CSRF token.
- **A missing, invalid or expired credential gets `401`.** So does a deactivated account, and a session with a missing or wrong `X-CSRF-Token`.
- **If Kitaru can't answer, the caller gets `503`,** never a pass.
- **The `Authorization` header wins.** When a request has the header and the cookie, the header is the credential and the cookie is ignored. Kitaru uses the same order.
- **Kitaru does the CSRF check.** The backend passes the cookie as a cookie, so Kitaru checks the CSRF token on every request, also on a `GET`. The backend has no CSRF check of its own. That check and the `SameSite=Lax` attribute of the cookie are the protection against cross-site requests.
- **Only the session cookie is passed on.** The backend does not send the caller's other cookies to Kitaru, and it keeps no cookie from a Kitaru answer.
- **Both ways have the same rights.** No route treats a dashboard session differently from a key.

The browser sends the cookie only to the host that set it. So the dashboard must reach the backend on its own host: `evalve-deploy` sends `/evalve/*` on `dashboard.<hostname>` to the backend. A session lasts 1 hour, and Kitaru does not renew it.

## Settings

Read from the environment when the process starts.

| Variable | Required | Default | Meaning |
| --- | --- | --- | --- |
| `EVALVE_KITARU_URL` | Yes | | The deployment's Kitaru server, for example `http://127.0.0.1:8000` |
| `EVALVE_DB_HOST` | Yes | | PostgreSQL host |
| `EVALVE_DB_USER` | Yes | | PostgreSQL user. It must be allowed to create a database |
| `EVALVE_DB_PWD` | Yes | | PostgreSQL password |
| `EVALVE_DB_PORT` | No | `5432` | |
| `EVALVE_DB_NAME` | No | `evalve` | The backend's own database. Created at startup if missing |
| `EVALVE_DB_SSL_MODE` | No | `verify-full` | Use `disable` for a local PostgreSQL without TLS |
| `EVALVE_DB_SSL_CA` | No | | Path to the CA bundle, for example the RDS bundle |
| `EVALVE_HOST` | No | `127.0.0.1` | Address to listen on |
| `EVALVE_PORT` | No | `8100` | Port to listen on |
| `EVALVE_DB_WAIT_SECONDS` | No | `120` | How long startup waits for PostgreSQL |
| `EVALVE_IMAGE` | No | | The image reference this container was started from. The health route reports it |
| `EVALVE_SESSION_COOKIE_NAME` | No | `evalve_session` | The name of Kitaru's dashboard session cookie. It must be the same as `KITARU_SERVER_AUTH_COOKIE_NAME`. An empty value turns cookie sign-in off |

## Run it locally

You need Python 3.11 or newer, a PostgreSQL server, and a Kitaru server.

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
```

```bash
EVALVE_KITARU_URL=http://localhost:8000 \
EVALVE_DB_HOST=localhost EVALVE_DB_USER=postgres EVALVE_DB_PWD=postgres \
EVALVE_DB_SSL_MODE=disable \
.venv/bin/python -m evalve_backend
```

Then check it:

```bash
curl http://127.0.0.1:8100/evalve/health
curl -H "Authorization: Bearer $KITARU_API_KEY" http://127.0.0.1:8100/evalve/v1/me
```

## Shutdown and restore

`evalve down` stops the server and takes a final snapshot of the PostgreSQL instance before destroying the infrastructure. The snapshot includes the separate `evalve` database alongside Kitaru's database. Restoring from that snapshot recovers the Loop state committed when the snapshot was taken. When the backend starts, it applies only schema versions missing from the restored database.

## Tests

```bash
.venv/bin/python -B -m unittest discover -s tests
```

The tests use a fake Kitaru server and a fake database connection, so they need neither. To also run the database tests against a real PostgreSQL, set `EVALVE_TEST_DB_HOST` (and `EVALVE_TEST_DB_PORT`, `EVALVE_TEST_DB_USER`, `EVALVE_TEST_DB_PWD` if they differ from `5432`, `postgres`, `postgres`). CI does this.

Regenerate the checked-in OpenAPI contract when a route changes:

```bash
PYTHONPATH=tests .venv/bin/python -c 'import json; from evalve_backend.app import create_app; from helpers import settings; print(json.dumps(create_app(settings()).openapi(), indent=2, sort_keys=True))' > openapi/evalve-backend.json
```

## Container

```bash
docker build -t evalve-backend .
```

The image runs `python -m evalve_backend` as a non-root user. On the Evalve server it shares the host network, listens on `127.0.0.1:8100`, and Caddy forwards `/evalve/*` to it.

## Publish

The backend image and `evalve-mcp` share one version number and one `v*` tag. Before you tag, set the same version in `pyproject.toml`, in `src/evalve_backend/__init__.py` and in `evalve-mcp/pyproject.toml`. CI fails when the three differ. The tag is also the version that a Teammate installs `evalve-mcp` from.

Publish from the `evalve-app/evalve-backend` repository. The Evalve server pulls the public image from GitHub's container registry by its exact digest.

1. Push a version tag, for example `v0.1.0`. The **Publish image** workflow builds the image and pushes it to `ghcr.io/evalve-app/evalve-backend`.
2. Open the workflow run. Its summary shows the pinned reference, in the form `ghcr.io/evalve-app/evalve-backend@sha256:...`.
3. The first time only: in the package's settings on GitHub, change its visibility to public. The server pulls without signing in.
4. Put the pinned reference in `evalve-deploy`, in both `infra/variables.tf` and `src/evalve/lifecycle.py`, then run `evalve up`.
