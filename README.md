# evalve-backend

The Evalve backend stores loop data. It runs on the same server as Kitaru and keeps its tables in its own database on the same PostgreSQL instance.

The backend applies pending schema migrations before it starts serving requests. Its Loop store preserves committed Loops, Experiments, and Runs across backend restarts. The HTTP API currently exposes the two routes below.

## Routes

Every route sits under `/evalve`, so one hostname can serve Kitaru and Evalve.

| Route | Sign-in | What it does |
| --- | --- | --- |
| `GET /evalve/health` | None | `200` with `"database": "ok"` when the backend can query its database. `503` when it can't. Both include `version` and `image`, so a deployment can confirm which build is serving. |
| `GET /evalve/v1/me` | Kitaru credential | `200 {"id": "...", "name": "..."}` for the Kitaru account behind the credential. |

## Sign-in

Evalve has no accounts of its own. A caller sends the same credential it would send to Kitaru:

```
Authorization: Bearer <Kitaru API key or token>
```

The backend passes that header to `GET /api/v1/accounts/me` on the Kitaru server named in `EVALVE_KITARU_URL` and accepts the request if Kitaru does.

- **Only the configured Kitaru server is asked.** Nothing in a request can change which server that is.
- **The credential is never stored or logged.** It is passed to Kitaru and dropped.
- **A missing, invalid or expired credential gets `401`.** So does a deactivated account.
- **If Kitaru can't answer, the caller gets `503`,** never a pass.

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

## Container

```bash
docker build -t evalve-backend .
```

The image runs `python -m evalve_backend` as a non-root user. On the Evalve server it shares the host network, listens on `127.0.0.1:8100`, and Caddy forwards `/evalve/*` to it.

## Publish

Publish from the `evalve-app/evalve-backend` repository. The Evalve server pulls the public image from GitHub's container registry by its exact digest.

1. Push a version tag, for example `v0.1.0`. The **Publish image** workflow builds the image and pushes it to `ghcr.io/evalve-app/evalve-backend`.
2. Open the workflow run. Its summary shows the pinned reference, in the form `ghcr.io/evalve-app/evalve-backend@sha256:...`.
3. The first time only: in the package's settings on GitHub, change its visibility to public. The server pulls without signing in.
4. Put the pinned reference in `evalve-deploy`, in both `infra/variables.tf` and `src/evalve/lifecycle.py`, then run `evalve up`.
