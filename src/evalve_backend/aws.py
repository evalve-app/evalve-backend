"""The AWS clients of the backend.

The backend has no static AWS keys. A client with no credentials gets the keys of the instance role
from IMDS, and from nowhere else: not the default chain of boto3, which would also take keys from the
environment or a profile. A client with credentials uses the keys of a session that the backend got
from `sts:AssumeRole`.

Each call of a client blocks, so a route makes it off the event loop. A call fails fast and is not
repeated by the SDK: the caller of the route repeats it.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import boto3
from botocore.config import Config
from botocore.credentials import InstanceMetadataFetcher, InstanceMetadataProvider
from botocore.exceptions import NoCredentialsError

CLIENT_CONFIG = Config(
    connect_timeout=3, read_timeout=10, retries={"mode": "standard", "total_max_attempts": 1},
)


@dataclass(frozen=True)
class AwsKeys:
    """The keys of the instance role or of an assumed role session. The secret and the token never go into a repr."""
    access_key_id: str
    secret_access_key: str = field(repr=False)
    session_token: str = field(repr=False)


# Builds the client of one AWS service in one region, with the instance role or with session keys.
ClientFactory = Callable[[str, str, AwsKeys | None], Any]


def instance_role() -> AwsKeys:
    """The keys of the instance role, read from IMDS for each client. Like an AWS call, the read is not repeated."""
    provider = InstanceMetadataProvider(iam_role_fetcher=InstanceMetadataFetcher(timeout=1, num_attempts=1))
    found = provider.load()
    if found is None:
        raise NoCredentialsError
    frozen = found.get_frozen_credentials()
    return AwsKeys(frozen.access_key, frozen.secret_key, frozen.token)


def client(service: str, region: str, credentials: AwsKeys | None = None) -> Any:
    """A client of one service. With no credentials, it uses the instance role. It can raise a BotoCoreError."""
    credentials = credentials or instance_role()
    key_arguments = {
        "aws_access_key_id": credentials.access_key_id,
        "aws_secret_access_key": credentials.secret_access_key,
        "aws_session_token": credentials.session_token,
    }
    # A new boto3 session for each client, because a session is not safe to share between threads.
    return boto3.session.Session().client(service, region_name=region, config=CLIENT_CONFIG, **key_arguments)
