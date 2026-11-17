# How does Kitaru 0.27.6 handle the dashboard session cookie?

Research for Shortcut story 349. The facts are for story 284 (the backend accepts the dashboard session cookie).
Date: 2026-11-17. No live server was called. Every answer comes from the documents below.

## Sources

All Kitaru citations point at the public repository `zenml-io/kitaru`, tag `python/kitaru/v0.27.6`
(commit `96dfb8236f09720a3d31a9c3a67cf4ac7e19ca91`). Paths below are relative to
<https://github.com/zenml-io/kitaru/tree/96dfb8236f09720a3d31a9c3a67cf4ac7e19ca91>.

- **Source**: `src/kitaru/server/...` (the server) and `tests/server/...` (its tests).
- **Deployment**: `evalve-deploy/infra/refresh.py:42-50` and `evalve-deploy/infra/bootstrap.sh.tftpl:36-44`, both at `origin/main` (commit `521005a`).
- **Dashboard**: `evalve-ui/src/api/client.ts` and `evalve-ui/docs/kitaru-ui-route-map.md:9-10`.
- **`evalve-ui` issue #57**: its answer covers the `KITKEY_` key only. On cookies it says one thing: "CSRF applies only to cookie sessions." It does not name the cookie.
- The Kitaru docs (`docs/book`) do not describe the auth cookie or the CSRF token (text search for "cookie" and "csrf"). The only prose is `CHANGELOG.md:541`.

"Verified" means the answer is read from source at that tag. It does not mean the behaviour was seen on the AWS server.

## Summary

| # | Question | Result |
|---|---|---|
| 1 | Cookie name and attributes | **Verified, with one correction.** Kitaru has no fixed cookie name. The name is a server setting, and the default is no cookie at all. The Evalve deployment sets it to `evalve_session`. Attributes: `HttpOnly`, `SameSite=Lax`, `Secure` (set by the deployment), `Path=/`, no `Domain`, `Max-Age` about 3600 s. |
| 2 | Does `/accounts/me` accept the cookie alone? | **Verified: no, not for a dashboard session.** Kitaru reads the cookie with no `Authorization` header, but a password-login session also needs the `X-CSRF-Token` header, on GET too. Without it the answer is 401. A server can send the cookie value as `Authorization: Bearer` and then needs no CSRF token. |
| 3 | CSRF protection | **Verified.** A per-session CSRF token inside the signed session token. The client gets it from the login answer (`csrf_token`) and sends it as `X-CSRF-Token` on every request. Kitaru checks it for cookie credentials only. `SameSite=Lax` is the second protection. Kitaru has no `Origin` or `Referer` check. |
| 4 | Can a caller tell cookie from API key in the answer? | **Verified: no.** No field or header of a Kitaru answer shows it. Kitaru itself does not keep the fact after the check. The backend must decide from the request it received. |

Changes this makes to story 284 are in [What changes for story 284](#what-changes-for-story-284).

## 1. The cookie's name and attributes

**Verified from source and from the deployment configuration.**

**Name.** Kitaru does not fix the name. It is the setting `AUTH_COOKIE_NAME`, default `""` (`src/kitaru/server/api/config.py:78`), environment variable `KITARU_SERVER_AUTH_COOKIE_NAME` (prefix at `src/kitaru/server/config.py:51`). With the default, the server sets no cookie, reads no cookie and issues no CSRF token (`.../adapters/rest/routers/auth.py:310`, `.../adapters/rest/dependencies/auth.py:200`, `.../adapters/auth/auth_service.py:351-355`). Kitaru's own tests use `kitaru_session` (`tests/server/test_auth_api.py:44`).

The Evalve deployment sets `KITARU_SERVER_AUTH_COOKIE_NAME=evalve_session` (`evalve-deploy/infra/refresh.py:47`). So `evalve_session` in the stories is correct **for this deployment**. It is a deployment choice, not a Kitaru constant. The backend must not hard-code it without a matching setting, or the two can drift.

**Attributes.** The cookie is set in one place, `POST /api/v1/login` (`.../adapters/rest/routers/auth.py:314-322`):

| Attribute | Value | Source |
|---|---|---|
| Value | The session token itself, a JWT. It is the same string as `access_token` in the login answer. | `routers/auth.py:316,324`; `tests/server/test_auth_api.py:395` |
| `HttpOnly` | Always. | `routers/auth.py:318` |
| `SameSite` | `Lax`, fixed. No setting changes it. | `routers/auth.py:319` |
| `Secure` | `AUTH_COOKIE_SECURE` if set, else true only when the request scheme is `https`. The deployment sets it to `true`. | `routers/auth.py:399-413`; `api/config.py:80`; `evalve-deploy/infra/refresh.py:48` |
| `Domain` | `AUTH_COOKIE_DOMAIN` if set, else none (a host-only cookie). The deployment does not set it. | `routers/auth.py:321`; `api/config.py:79`; `evalve-deploy/infra/refresh.py:33-51` |
| `Path` | Kitaru passes no path, so the framework default applies. That default is `/` in Starlette's `Response.set_cookie`. **Not confirmed at the tag**: Starlette is a dependency (locked at 1.3.1, `uv.lock:3680-3681`), not part of the Kitaru source. | `routers/auth.py:314-322` |
| Lifetime | `Max-Age` = the token's remaining life in whole seconds. The token lives `JWT_LIFETIME_SECONDS`, default 3600. The deployment does not change it. So `Max-Age` is 3600 or 3599. | `routers/auth.py:307,317`; `api/config.py:75`; `auth_service.py:282-284` |

Other facts about the cookie:

- **Which logins set it.** Only the `password` and `control-plane` grants. A `device_code` or `api-key` login never sets the cookie (`routers/auth.py:308-313`; `tests/server/test_auth_api.py:351-372`).
- **No renewal.** The server has no refresh route and no sliding expiry. After one hour the token is invalid and the person signs in again.
- **Logout does not revoke.** `POST /api/v1/logout` only tells the browser to delete the cookie (`routers/auth.py:331-347`). The session token is a stateless JWT, so a copy stays valid until it expires. The checks at each request are the signature, the expiry and that the account is still active (`auth_service.py:408-413,443-453`).
- **Same origin.** The deployment serves everything from one host through Caddy (`evalve-deploy/infra/bootstrap.sh.tftpl:41-43`). A host-only cookie with `Path=/` goes to every path on that host. If the backend is served on the same host, the browser sends the cookie to it too. If it is on another host, the browser does not send it, and `AUTH_COOKIE_DOMAIN` would be needed.

**Not verifiable without a live test**: the exact `Set-Cookie` line on the AWS server. Test: `POST /api/v1/login` with a username and password, and read the `Set-Cookie` header. Expected: `evalve_session=<jwt>; HttpOnly; Max-Age=3600; Path=/; SameSite=lax; Secure`.

## 2. Does `GET /api/v1/accounts/me` accept the cookie alone?

**Verified from source: Kitaru reads the cookie with no `Authorization` header, but a dashboard session then also needs the CSRF header.**

How Kitaru finds the credential (`.../adapters/rest/dependencies/auth.py:182-204`):

1. If the request has `Authorization: Bearer <x>`, that is the credential and `from_cookie` is false. The cookie is ignored.
2. Else, if `AUTH_COOKIE_NAME` is set and the request has that cookie, the cookie value is the credential and `from_cookie` is true.
3. Else 401 `Missing bearer credential.` (`dependencies/auth.py:304-308`).

`GET /api/v1/accounts/me` uses the common `authorize` dependency (`.../adapters/rest/routers/accounts.py:69-83`; `dependencies/auth.py:353-367`). It has no rule of its own.

Then the CSRF check (`.../adapters/auth/auth_service.py:140-149`): if the credential came from the cookie **and** the token carries a CSRF token, the `X-CSRF-Token` header must equal it. If not, the answer is 401 `Missing or invalid CSRF token.` The check does not look at the HTTP method, so it applies to GET.

So, for a server (not a browser) that calls `/accounts/me`:

| What the server sends | Answer |
|---|---|
| `Cookie: evalve_session=<dashboard token>`, nothing else | **401** `Missing or invalid CSRF token.` |
| `Cookie: evalve_session=<dashboard token>` and `X-CSRF-Token: <csrf_token>` | **200** |
| `Authorization: Bearer <dashboard token>` (the cookie value moved to the header), no CSRF header | **200** |

The third row is from the source comment "a token that carries a CSRF token still resolves without one there" (`auth_service.py:140-142`) and from the tests `test_header_credential_skips_csrf_check` (`tests/server/test_auth_api.py:411-436`) and `test_csrf_token_not_required_for_header_credentials` (`tests/server/test_auth_service.py:348-366`). The first two rows are from `test_cookie_login_and_logout` (`tests/server/test_auth_api.py:398-404`), which uses `/api/v1/api-keys`, a route with the same dependency.

Kitaru does not care who sends the request. It has no check of `Origin`, `User-Agent` or `X-Kitaru-Client`. A server and a browser get the same answer.

A dashboard token always carries a CSRF token on this deployment: a password login makes one when `AUTH_COOKIE_NAME` is set (`auth_service.py:195,351-355`).

**Two more cases, both accepted by Kitaru with the cookie alone:**

- A cookie whose value is a `KITKEY_` key. Kitaru decides the credential type by prefix, wherever the value came from (`auth_service.py:136-139`). An API key has no CSRF token, so the check passes.
- A cookie whose value is a token from the `api-key` or `device_code` grant. These tokens carry no CSRF token (`auth_service.py:213,257-261`), so the check passes (`tests/server/test_auth_service.py:369-388`).

A browser never gets such a cookie from Kitaru. A script or a person can build one. This matters for question 4.

**Not verifiable without a live test**: nothing in the logic. Smoke test on the AWS server: the three rows of the table above.

## 3. CSRF and other cross-site protection

**Verified from source.** Kitaru has two protections. Both exist only when `AUTH_COOKIE_NAME` is set.

**The CSRF token.**

- **Made at login.** 32 hex characters from `secrets.token_hex(16)`, one per login (`auth_service.py:351-355`).
- **Stored in the session token.** It is the `csrf` claim of the signed JWT (`.../adapters/auth/jwt.py:85-121`). The server keeps no session table.
- **How a client gets it.** Only from the body of the login answer: `csrf_token` in `TokenResponse` (`routers/auth.py:323-328`; `src/kitaru/api_models/v1/auth.py:54-55`). No other route returns it. The cookie is `HttpOnly`, so page script cannot read the claim from the cookie.
- **How a client sends it.** The header `X-CSRF-Token` (`dependencies/auth.py:65,197`).
- **When Kitaru checks it.** On every authenticated request, of every method, when the credential came from the cookie and the token has a `csrf` claim. The compare is constant-time (`auth_service.py:143-148`). A failure is 401, not 403.
- **When Kitaru does not check it.** For a credential in the `Authorization` header. The reason is in the source: "Only a cookie rides along on a cross-site request" (`auth_service.py:140-142`; `CHANGELOG.md:541`).
- **Lifetime.** The same as the session token. It does not rotate.

The dashboard does this today: it reads `csrf_token` from the login answer, keeps it in `localStorage` under `evalve.csrf-token`, and adds `X-CSRF-Token` to every call (`evalve-ui/src/api/client.ts:27-52,78-90`). So page script in the dashboard can read the CSRF token, but not the cookie.

**`SameSite=Lax`.** Fixed in the source (`routers/auth.py:319`). The browser does not send the cookie on a cross-site `POST`, `PUT`, `PATCH` or `DELETE`, or on cross-site `fetch` calls. It does send it on a cross-site top-level `GET` navigation. That GET still fails at the CSRF check.

**CORS.** `CORSMiddleware` allows all methods and headers for the configured origins, with credentials when the list has no `*` (`src/kitaru/server/api/app.py:386-392`). The deployment sets the list to its own URL (`evalve-deploy/infra/refresh.py:49`). CORS is a browser rule on reading answers. It is not a server-side refusal.

**What Kitaru does not have.** No `Origin` or `Referer` check, no double-submit cookie, and no CSRF protection on the login route itself (text search of `src/kitaru/server` for `origin`, `referer`, `csrf`).

## 4. Can a caller tell from the answer which credential was used?

**Verified from source: no.**

- `GET /api/v1/accounts/me` returns `account_to_response(actor.account)` only (`routers/accounts.py:69-83`). The earlier research found the same (`docs/research/kitaru-runner-assumptions.md`, section 6).
- Kitaru itself drops the fact. `from_cookie` exists only inside `resolve` for the CSRF check. The resolved `AuthContext` holds `account`, `principal` and `csrf_token`; it has no field for the credential type or its source (`.../application/models/auth.py:68-77`; `auth_service.py:434,453`). An API key and a session token for one account give an equal context, except for `csrf_token`.
- No response header shows it. `Set-Cookie` appears only on the login answer.
- The answer is the same account body for a cookie session, a Bearer session token and a `KITKEY_` key.

**What the backend can use instead.** The backend holds the request, so it can decide itself:

1. **Where the credential came from.** The `evalve_session` cookie or the `Authorization` header. But section 2 shows that a cookie can carry an API key or a headless token, so the source alone is not proof of a dashboard login.
2. **The `KITKEY_` prefix.** Kitaru uses the same test (`auth_service.py:136`). A cookie value with this prefix is an API key.
3. **Make Kitaru prove the CSRF token.** Forward the cookie as a cookie, with the caller's `X-CSRF-Token`, and not as `Authorization: Bearer`. Then Kitaru checks the CSRF token. This alone does not prove a dashboard login either, because Kitaru skips the check for a token with no `csrf` claim.
4. **Read the `csrf` claim.** The session token is a JWT, and its payload is readable without the signing key. The backend can require that the payload has a `csrf` claim, and that it equals the `X-CSRF-Token` header. Kitaru's 200 then proves the signature. A token with a `csrf` claim comes only from a `password` or `control-plane` login on a cookie server (`auth_service.py:195,228,351-355`). This is the only way found to tell "a person signed in with a password" from "an API key or a token made from one". It depends on Kitaru's token format, which the source calls a wire format that must stay backward compatible (`jwt.py:82-84`).

**A limit that stays.** A person can sign in with the password from a script, and then holds the token and the CSRF token. Kitaru cannot tell that from a browser. "Cookie session" can mean at most "signed in with the account's password".

## What changes for story 284

1. **The cookie name is right for this deployment, but it is a setting.** `evalve_session` comes from `evalve-deploy/infra/refresh.py:47`, not from Kitaru. Give the backend a setting for the name with `evalve_session` as the default, or state the link in both repositories.
2. **The cookie alone is not enough.** The story says "a request with a valid dashboard session cookie and no `Authorization` header gets the account". Kitaru answers 401 to that cookie without `X-CSRF-Token`. The dashboard already sends the header on every call (`evalve-ui/src/api/client.ts:46-52`), so the backend can require it and forward it.
3. **Choose how to forward, because the choice is the CSRF protection.**
   - Forward as `Cookie` plus `X-CSRF-Token`: Kitaru checks the CSRF token on every request, GET included. The backend gets Kitaru's CSRF protection with no code of its own. This is the safer choice.
   - Forward as `Authorization: Bearer <cookie value>`: Kitaru skips the CSRF check. The backend then has no CSRF protection except `SameSite=Lax`, and must add its own. Do not do this without a check in the backend.
4. **The 401 text changes.** A missing or wrong CSRF token is 401 from Kitaru, with detail `Missing or invalid CSRF token.` The backend's rule "401 or 403 from Kitaru means not signed in" (`src/evalve_backend/auth.py:47-48`) already covers it.
5. **"Each route can tell which way the caller signed in" needs a definition.** Kitaru's answer cannot tell. Use the request: the source (cookie or header), the `KITKEY_` prefix, and for the approval route ([sc-260](https://app.shortcut.com/team-4-1/story/260)) the `csrf` claim (option 4 in section 4). Without the claim check, a caller can put a `KITKEY_` key or an api-key token in a cookie named `evalve_session` and pass as a cookie session.
6. **When both a cookie and an `Authorization` header arrive, the header wins in Kitaru** (`dependencies/auth.py:196-199`). The backend should use the same order, so that the two never disagree about the caller.
7. **Never log the cookie or the CSRF token.** The cookie value is a full Bearer credential for up to one hour, and logout does not revoke it.
8. **The session lasts one hour with no renewal.** A long page in the dashboard gets 401 from the backend at the same moment as from Kitaru.
9. **The same-host rule.** The browser sends the cookie to the backend only if the backend is on the dashboard's host (host-only cookie, `Path=/`). The Caddy file at `origin/main` routes everything to Kitaru (`evalve-deploy/infra/bootstrap.sh.tftpl:41-43`); a route for the backend on that host is needed.
