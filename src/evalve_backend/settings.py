"""Settings read from the environment once, when the process starts."""

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal, get_args
from urllib.parse import urlsplit

# Where the Runs of a Loop execute. With `fargate` the backend also gives push credentials for the
# agent images, so it reads the AWS settings; with `kitaru` it reads none of them.
Executor = Literal["kitaru", "fargate"]

# An ECR repository URI: the registry host of an AWS account and region, then the repository name.
_ECR_REPOSITORY = re.compile(
    r"(?P<registry>[0-9]{12}\.dkr\.ecr\.[a-z0-9-]+\.amazonaws\.com(?:\.cn)?)"
    r"/(?P<name>(?:[a-z0-9]+(?:[._-][a-z0-9]+)*/)*[a-z0-9]+(?:[._-][a-z0-9]+)*)"
)
_AWS_REGION = re.compile(r"[a-z]{2}(?:-[a-z]+)+-[0-9]")
_IAM_ROLE_ARN = re.compile(r"arn:aws[a-z-]*:iam::[0-9]{12}:role/[\w+=,.@/-]{1,512}")


class SettingsError(Exception):
    """A setting is missing or invalid. The message never includes a value."""


@dataclass(frozen=True)
class Settings:
    # The deployment's own Kitaru server. Sign-in checks go here and nowhere else.
    kitaru_url: str
    db_host: str
    db_user: str
    db_password: str = field(repr=False)
    db_port: int = 5432
    db_name: str = "evalve"
    db_ssl_mode: str = "verify-full"
    db_ssl_ca: str | None = None
    host: str = "127.0.0.1"
    port: int = 8100
    # The image this process was started from, when the deployment tells it.
    image: str | None = None
    # The name of Kitaru's dashboard session cookie. The deployment sets the same name for Kitaru
    # (KITARU_SERVER_AUTH_COOKIE_NAME). An empty value turns cookie sign-in off.
    session_cookie_name: str = "evalve_session"
    executor: Executor = "kitaru"
    # The settings below are read only with the executor `fargate`, and then each one is set.
    aws_region: str | None = None
    # The URI of the one ECR repository of the agent images.
    agent_image_repository: str | None = None
    # The push-only role that the backend assumes to give a builder an ECR token (sc-487).
    agent_image_push_role_arn: str | None = None

    @property
    def agent_image_registry(self) -> str | None:
        return _repository_part(self.agent_image_repository, "registry")

    @property
    def agent_image_repository_name(self) -> str | None:
        return _repository_part(self.agent_image_repository, "name")

    @classmethod
    def from_environment(cls, env: Mapping[str, str] | None = None) -> "Settings":
        env = os.environ if env is None else env
        required = ("EVALVE_KITARU_URL", "EVALVE_DB_HOST", "EVALVE_DB_USER", "EVALVE_DB_PWD")
        missing = [name for name in required if not env.get(name, "").strip()]
        if missing:
            raise SettingsError("Set " + ", ".join(missing) + ".")
        kitaru_url = env["EVALVE_KITARU_URL"].strip().rstrip("/")
        try:
            target = urlsplit(kitaru_url)
            valid = (
                target.scheme in ("http", "https") and bool(target.hostname)
                and target.username is None and target.password is None
                and not (target.query or target.fragment)
            )
        except ValueError:
            valid = False
        if not valid:
            raise SettingsError(
                "EVALVE_KITARU_URL must be an HTTP(S) URL without credentials, a query, or a fragment."
            )
        db_name = env.get("EVALVE_DB_NAME", "evalve")
        if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", db_name):
            raise SettingsError("EVALVE_DB_NAME must be a lowercase PostgreSQL identifier.")
        cookie_name = env.get("EVALVE_SESSION_COOKIE_NAME", "evalve_session")
        # The name goes into a Cookie header, so it must be a plain cookie name.
        if cookie_name and not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", cookie_name):
            raise SettingsError("EVALVE_SESSION_COOKIE_NAME must be a cookie name of letters, digits, '_', '.' or '-'.")
        executor = env.get("EVALVE_EXECUTOR", "kitaru")
        if executor not in get_args(Executor):
            raise SettingsError("EVALVE_EXECUTOR must be kitaru or fargate.")
        aws = _aws_settings(env) if executor == "fargate" else {}
        return cls(
            kitaru_url=kitaru_url,
            db_host=env["EVALVE_DB_HOST"].strip(),
            db_user=env["EVALVE_DB_USER"].strip(),
            db_password=env["EVALVE_DB_PWD"],
            db_port=port_setting(env, "EVALVE_DB_PORT", 5432),
            db_name=db_name,
            db_ssl_mode=env.get("EVALVE_DB_SSL_MODE", "verify-full"),
            db_ssl_ca=env.get("EVALVE_DB_SSL_CA") or None,
            host=env.get("EVALVE_HOST", "127.0.0.1"),
            port=port_setting(env, "EVALVE_PORT", 8100),
            image=env.get("EVALVE_IMAGE") or None,
            session_cookie_name=cookie_name,
            executor=executor,
            **aws,
        )


def _aws_settings(env: Mapping[str, str]) -> dict:
    required = ("EVALVE_AWS_REGION", "EVALVE_AGENT_IMAGE_REPOSITORY", "EVALVE_AGENT_IMAGE_PUSH_ROLE_ARN")
    missing = [name for name in required if not env.get(name, "").strip()]
    if missing:
        raise SettingsError("With EVALVE_EXECUTOR=fargate, set " + ", ".join(missing) + ".")
    region, repository, role_arn = (env[name].strip() for name in required)
    if not _AWS_REGION.fullmatch(region):
        raise SettingsError("EVALVE_AWS_REGION must be an AWS region, for example eu-central-1.")
    if not _ECR_REPOSITORY.fullmatch(repository):
        raise SettingsError(
            "EVALVE_AGENT_IMAGE_REPOSITORY must be the URI of an ECR repository, "
            "<account>.dkr.ecr.<region>.amazonaws.com/<name>."
        )
    if not _IAM_ROLE_ARN.fullmatch(role_arn):
        raise SettingsError("EVALVE_AGENT_IMAGE_PUSH_ROLE_ARN must be the ARN of an IAM role.")
    return {"aws_region": region, "agent_image_repository": repository, "agent_image_push_role_arn": role_arn}


def _repository_part(repository: str | None, part: str) -> str | None:
    match = _ECR_REPOSITORY.fullmatch(repository) if repository else None
    return match[part] if match else None


def port_setting(env: Mapping[str, str], name: str, default: int) -> int:
    value = env.get(name, str(default))
    if not value.isdecimal() or not 1 <= int(value) <= 65535:
        raise SettingsError(f"{name} must be a port number.")
    return int(value)
