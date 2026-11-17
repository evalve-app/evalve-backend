"""Sign-in: ask the deployment's own Kitaru server who the caller is.

Evalve has no accounts of its own. A caller sends the same Kitaru credential it
would send to Kitaru, and Kitaru decides whether it is valid. The credential is
passed through to Kitaru and is never stored or logged.
"""

import logging
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

ACCOUNT_PATH = "/api/v1/accounts/me"
MAX_HEADER_LENGTH = 4096


@dataclass(frozen=True)
class Account:
    id: str
    name: str


class NotSignedIn(Exception):
    """The caller sent no credential, or Kitaru rejected it."""


class KitaruUnavailable(Exception):
    """Kitaru could not answer, so the caller's identity is unknown."""


async def current_account(kitaru: httpx.AsyncClient, authorization: str | None) -> Account:
    """Return the Kitaru account behind an Authorization header.

    `kitaru` is the client for the configured Kitaru server. The caller has no
    say in which server is asked.
    """
    if not _is_bearer(authorization):
        raise NotSignedIn
    try:
        response = await kitaru.get(ACCOUNT_PATH, headers={"Authorization": authorization})
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
