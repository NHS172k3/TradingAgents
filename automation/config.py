"""Typed, validated configuration for the on-demand bot service.

All environment reads for the service live here. Everything else takes an
injected :class:`ServiceConfig` instance — no scattered ``os.environ`` reads,
no module-level globals.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# Load .env from repo root if python-dotenv is available
try:
    from dotenv import load_dotenv, find_dotenv
    load_dotenv(find_dotenv(usecwd=True))
except ImportError:
    pass

from automation import settings

DEFAULT_DAILY_CAP = 5
DEFAULT_PRESET = "cost_saver"
DEFAULT_WEB_HOST = "127.0.0.1"
DEFAULT_WEB_PORT = 8787
DEFAULT_DB_PATH = settings.LOGS_DIR / "service.db"


class ConfigError(ValueError):
    """Raised when required service configuration is missing or invalid."""


@dataclass(frozen=True)
class ServiceConfig:
    """Validated configuration for the on-demand bot + report-hosting service."""

    bot_token: str
    invite_code: str
    public_base_url: str
    daily_cap: int = DEFAULT_DAILY_CAP
    preset: str = DEFAULT_PRESET
    web_host: str = DEFAULT_WEB_HOST
    web_port: int = DEFAULT_WEB_PORT
    db_path: Path = DEFAULT_DB_PATH
    admin_user_id: Optional[int] = None

    @staticmethod
    def from_env() -> "ServiceConfig":
        """Build a ServiceConfig from environment variables.

        Raises:
            ConfigError: if a required variable is missing, or an optional
                variable is set but invalid (e.g. non-integer cap/port,
                unknown preset).
        """
        bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        invite_code = os.environ.get("TELEGRAM_INVITE_CODE", "").strip()
        public_base_url = os.environ.get("REPORTS_PUBLIC_BASE_URL", "").strip()

        missing = []
        if not bot_token:
            missing.append("TELEGRAM_BOT_TOKEN")
        if not invite_code:
            missing.append("TELEGRAM_INVITE_CODE")
        if not public_base_url:
            missing.append("REPORTS_PUBLIC_BASE_URL")
        if missing:
            raise ConfigError(
                "Missing required environment variable(s): " + ", ".join(missing)
            )

        preset = os.environ.get("BOT_PRESET", DEFAULT_PRESET).strip()
        if preset not in settings.PRESETS:
            raise ConfigError(
                f"BOT_PRESET={preset!r} is not a known preset "
                f"(valid: {', '.join(sorted(settings.PRESETS))})"
            )

        daily_cap = _parse_int("BOT_DAILY_CAP", DEFAULT_DAILY_CAP)
        if daily_cap < 1:
            raise ConfigError("BOT_DAILY_CAP must be at least 1")

        web_port = _parse_int("REPORTS_WEB_PORT", DEFAULT_WEB_PORT)
        if web_port < 1 or web_port > 65535:
            raise ConfigError("REPORTS_WEB_PORT must be between 1 and 65535")

        admin_raw = os.environ.get("BOT_ADMIN_USER_ID", "").strip()
        admin_user_id: Optional[int] = None
        if admin_raw:
            try:
                admin_user_id = int(admin_raw)
            except ValueError:
                raise ConfigError(
                    f"BOT_ADMIN_USER_ID={admin_raw!r} is not a valid integer"
                )

        db_path_raw = os.environ.get("BOT_DB_PATH", "").strip()
        db_path = Path(db_path_raw).expanduser() if db_path_raw else DEFAULT_DB_PATH

        web_host = os.environ.get("REPORTS_WEB_HOST", DEFAULT_WEB_HOST).strip()
        if not web_host:
            web_host = DEFAULT_WEB_HOST

        return ServiceConfig(
            bot_token=bot_token,
            invite_code=invite_code,
            public_base_url=public_base_url.rstrip("/"),
            daily_cap=daily_cap,
            preset=preset,
            web_host=web_host,
            web_port=web_port,
            db_path=db_path,
            admin_user_id=admin_user_id,
        )


def _parse_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise ConfigError(f"{name}={raw!r} is not a valid integer")
