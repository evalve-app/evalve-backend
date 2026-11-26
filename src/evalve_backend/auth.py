"""Sign-in: ask the deployment's own Kitaru server who the caller is.

Evalve has no accounts of its own. A caller sends the same Kitaru credential it
would send to Kitaru, and Kitaru decides whether it is valid. The credential is
passed through to Kitaru and is never stored or logged.

A caller signs in one of two ways:

- `Authorization: Bearer <Kitaru API key or token>`. The Evalve MCP does this.
- The dashboard session: Kitaru's session cookie with the `X-CSRF-Token` header.
  The backend passes both to Kitaru as they are, and Kitaru checks the CSRF token
  on every request. The backend has no CSRF check of its own.

When a request has an `Authorization` header, that header is the credential and
the cookie is ignored. Kitaru uses the same order.

The two ways have the same rights, with one exception: only a Bearer credential
starts a Run. `Credential.kind` gives the type, and `loops_api.can_drive` is the
one function that reads it.
"""

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Annotated, Literal

import httpx
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

logger = logging.getLogger(__name__)

ACCOUNT_PATH = "/api/v1/accounts/me"
MAX_HEADER_LENGTH = 4096
CSRF_HEADER = "X-CSRF-Token"
# What a cookie value can hold (RFC 6265). It has no space, quote, comma, semicolon or backslash, so
# a value that matches cannot add a second cookie or a second header to the request for Kitaru.
_COOKIE_VALUE = re.compile(r"[\x21\x23-\x2B\x2D-\x3A\x3C-\x5B\x5D-\x7E]+")


@dataclass(frozen=True)
class Account:
    id: str
    name: str


@dataclass(frozen=True)
class Credential:
    """What the caller sent to prove who it is: the headers to send to Kitaru for this caller.

    Either `Authorization`, or `Cookie` with `X-CSRF-Token`. The values never go into a repr.
    `kind` is `bearer` for a key or a token and `session` for the dashboard session.
    """

    headers: Mapping[str, str] = field(repr=False)
    kind: Literal["bearer", "session"]

    @classmethod
    def bearer(cls, authorization: str) -> "Credential":
        return cls(MappingProxyType({"Authorization": authorization}), "bearer")

    @classmethod
    def session(cls, cookie_name: str, cookie_value: str, csrf_token: str | None) -> "Credential":
        headers = {"Cookie": f"{cookie_name}={cookie_value}"}
        if csrf_token is not None:
            headers[CSRF_HEADER] = csrf_token
        return cls(MappingProxyType(headers), "session")


def read_credential(request: Request, cookie_name: str) -> Credential | None:
    """The credential of a request, or None when it has none that can be passed to Kitaru.

    A session with no `X-CSRF-Token` header is passed on without one, and Kitaru then refuses a
    dashboard session. Only the session cookie is passed on, never the caller's other cookies.
    """
    authorization = request.headers.get("authorization")
    if authorization is not None:
        # The header is the credential, valid or not. The cookie is not a second try.
        return Credential.bearer(authorization) if _is_bearer(authorization) else None
    if not cookie_name:
        return None
    session = request.cookies.get(cookie_name)
    if not _is_cookie_value(session):
        return None
    csrf_token = request.headers.get(CSRF_HEADER)
    if csrf_token is not None and not _is_cookie_value(csrf_token):
        return None
    return Credential.session(cookie_name, session, csrf_token)


def request_credential(request: Request) -> Credential | None:
    return read_credential(request, request.app.state.session_cookie_name)


class NotSignedIn(Exception):
    """The caller sent no credential, or Kitaru rejected it."""


class KitaruUnavailable(Exception):
    """Kitaru could not answer, so the caller's identity is unknown."""


async def current_account(kitaru: httpx.AsyncClient, credential: Credential | None) -> Account:
    """Return the Kitaru account behind a credential.

    `kitaru` is the client for the configured Kitaru server. The caller has no
    say in which server is asked.
    """
    if credential is None:
        raise NotSignedIn
    try:
        response = await kitaru.get(ACCOUNT_PATH, headers=dict(credential.headers))
    except httpx.HTTPError as error:
        # Log the kind of failure only. An httpx error can carry the request.
        logger.warning("Kitaru did not answer the sign-in check (%s).", type(error).__name__)
        raise KitaruUnavailable from None
    if response.status_code in (401, 403):
        raise NotSignedIn
    if response.status_code != 200:
        logger.warning("Kitaru answered the sign-in check with HTTP %d.", response.status_code)
        raise KitaruUnavailable
    try:
        account = response.json()
        account_id, name, active = account["id"], account["name"], account["active"]
    except (ValueError, TypeError, KeyError):
        logger.warning("Kitaru answered the sign-in check with an unexpected body.")
        raise KitaruUnavailable from None
    # Kitaru always sends these fields. Anything else is not an answer to trust.
    if not (isinstance(account_id, str) and account_id and isinstance(name, str) and isinstance(active, bool)):
        logger.warning("Kitaru answered the sign-in check with an unexpected body.")
        raise KitaruUnavailable
    if not active:
        raise NotSignedIn
    return Account(id=account_id, name=name)


def _is_bearer(authorization: str | None) -> bool:
    if not authorization or len(authorization) > MAX_HEADER_LENGTH:
        return False
    # A header that is not printable ASCII cannot be forwarded and is not a Kitaru credential.
    if not authorization.isascii() or not authorization.isprintable():
        return False
    scheme, _, credential = authorization.partition(" ")
    return scheme.lower() == "bearer" and bool(credential.strip())


def _is_cookie_value(value: str | None) -> bool:
    return bool(value) and len(value) <= MAX_HEADER_LENGTH and _COOKIE_VALUE.fullmatch(value) is not None


async def require_account(
    request: Request,
    bearer: Annotated[HTTPAuthorizationCredentials | None, Depends(HTTPBearer(auto_error=False))],
) -> Account:
    """Resolve the caller through Kitaru, or refuse the request.

    `bearer` is here so that the API contract names the Bearer scheme. `read_credential` reads the request.
    """
    return await verify_account(request)


async def verify_account(request: Request) -> Account:
    try:
        return await current_account(request.app.state.kitaru, request_credential(request))
    except NotSignedIn:
        raise HTTPException(
            status_code=401,
            detail=(
                "Sign in to Kitaru. Send its credential as a Bearer token, or send the dashboard "
                "session cookie with the X-CSRF-Token header."
            ),
            headers={"WWW-Authenticate": "Bearer"},
        ) from None
    except KitaruUnavailable:
        raise HTTPException(
            status_code=503, detail="Kitaru could not confirm who is calling. Try again."
        ) from None
