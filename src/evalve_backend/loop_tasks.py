"""The rules for the channel between a Loop task and the backend. No I/O.

The Supervisor in a Loop task polls the backend for its Assignments, and it shows the secret of the
Loop task. The backend holds only the SHA-256 of the secret.
"""

from hashlib import sha256

from evalve_backend.auth import MAX_HEADER_LENGTH

SCHEME = "EvalveLoopTask"
# The contract versions of the Supervisor that this backend supports.
MIN_CONTRACT_VERSION = 1
MAX_CONTRACT_VERSION = 1


def hash_secret(secret: str) -> str:
    """What the backend stores and compares in place of the secret."""
    return sha256(secret.encode()).hexdigest()


def read_secret(authorization: str | None) -> str | None:
    """The secret in an `Authorization: EvalveLoopTask <secret>` header, or None for each other header."""
    if not authorization or len(authorization) > MAX_HEADER_LENGTH:
        return None
    if not authorization.isascii() or not authorization.isprintable():
        return None
    scheme, _, secret = authorization.partition(" ")
    secret = secret.strip()
    return secret if scheme.lower() == SCHEME.lower() and secret else None


def refused_version_reason(contract_version: int) -> str | None:
    """Why the backend refuses a contract version, or None when it supports the version."""
    if MIN_CONTRACT_VERSION <= contract_version <= MAX_CONTRACT_VERSION:
        return None
    return (f"The Supervisor of the agent image uses contract version {contract_version}. "
            f"The backend supports versions {MIN_CONTRACT_VERSION} to {MAX_CONTRACT_VERSION}.")
