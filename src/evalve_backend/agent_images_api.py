"""The push credential for the agent images: contract version 1 of `POST /v1/agent-images/push-credential`.

The `evalve build` command pushes an agent image with only a Kitaru API key, so it asks the backend
for an ECR token. An ECR token is as wide as its caller, so the backend never gives one of the
instance role. It assumes the push-only role, which can push only to the one repository, and gets
the token with the keys of that session. Each call makes a new session, and the backend stores
nothing. The token lives 12 hours, longer than the session of 900 s; the pilot accepts this.

The password never goes into a log line or an error reply.
"""

import base64
import logging
from asyncio import to_thread
from datetime import datetime
from typing import Annotated

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from evalve_backend.auth import Account, require_account
from evalve_backend.aws import ClientFactory, SessionCredentials
from evalve_backend.settings import Settings

logger = logging.getLogger(__name__)

router = APIRouter()

SESSION_SECONDS = 900
NO_REPOSITORY = "This deployment has no agent image repository."
AWS_UNAVAILABLE = "AWS could not give a push credential. Try again."


class PushCredential(BaseModel):
    """What `docker login` needs to push to the one agent image repository."""
    registry: str
    repository: str
    username: str
    password: str
    expires_at: datetime


class PushCredentialUnavailable(Exception):
    """AWS refused or did not answer. The reason is in a log line, with no credential."""


def session_name(account_id: str) -> str:
    # CloudTrail shows the session name, so the account behind each push session can be found there.
    return f"evalve-push-{account_id}"


def push_credential(settings: Settings, aws_client: ClientFactory, account_id: str) -> PushCredential:
    """Assume the push-only role for the account, and get an ECR token with the keys of that session."""
    region = settings.aws_region
    try:
        sts = aws_client("sts", region, None)
        role = sts.assume_role(
            RoleArn=settings.agent_image_push_role_arn, RoleSessionName=session_name(account_id),
            DurationSeconds=SESSION_SECONDS,
        )["Credentials"]
        keys = SessionCredentials(role["AccessKeyId"], role["SecretAccessKey"], role["SessionToken"])
        [authorization] = aws_client("ecr", region, keys).get_authorization_token()["authorizationData"]
        token = base64.b64decode(authorization["authorizationToken"], validate=True).decode()
        expires_at = authorization["expiresAt"]
    except ClientError as error:
        # The code names the refusal. The message of the error is not logged.
        logger.warning("AWS refused a push credential (%s).", error.response.get("Error", {}).get("Code"))
        raise PushCredentialUnavailable from None
    except (BotoCoreError, KeyError, TypeError, ValueError) as error:
        logger.warning("AWS did not give a push credential (%s).", type(error).__name__)
        raise PushCredentialUnavailable from None
    username, _, password = token.partition(":")
    if username != "AWS" or not password:
        logger.warning("ECR answered with a token that is not for the user AWS.")
        raise PushCredentialUnavailable
    logger.info("Gave a push credential for the agent images to the account %s. It expires at %s.",
                account_id, expires_at.isoformat())
    return PushCredential(
        registry=settings.agent_image_registry, repository=settings.agent_image_repository_name,
        username=username, password=password, expires_at=expires_at,
    )


@router.post("/v1/agent-images/push-credential")
async def create_push_credential(
    request: Request, response: Response, account: Annotated[Account, Depends(require_account)],
) -> PushCredential:
    """A short-lived ECR credential that can push to the one agent image repository and do nothing else.

    Each active Kitaru account can call it. The reply is new for each call and the backend keeps no copy.
    """
    settings: Settings = request.app.state.settings
    if settings.executor != "fargate":
        raise HTTPException(status_code=404, detail=NO_REPOSITORY)
    try:
        credential = await to_thread(push_credential, settings, request.app.state.aws_client, account.id)
    except PushCredentialUnavailable:
        raise HTTPException(status_code=503, detail=AWS_UNAVAILABLE) from None
    # The reply holds a password, so no cache keeps it.
    response.headers["Cache-Control"] = "no-store"
    return credential
