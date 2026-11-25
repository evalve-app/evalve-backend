"""Settings read from the environment once, when the process starts."""

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import urlsplit


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
        )


def port_setting(env: Mapping[str, str], name: str, default: int) -> int:
    value = env.get(name, str(default))
    if not value.isdecimal() or not 1 <= int(value) <= 65535:
        raise SettingsError(f"{name} must be a port number.")
    return int(value)
