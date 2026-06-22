"""Tests for automation.config.ServiceConfig.from_env."""

from __future__ import annotations

from pathlib import Path

import pytest

from automation.config import (
    DEFAULT_DAILY_CAP,
    DEFAULT_DB_PATH,
    DEFAULT_PRESET,
    DEFAULT_WEB_HOST,
    DEFAULT_WEB_PORT,
    ConfigError,
    ServiceConfig,
)

_SERVICE_ENV_VARS = (
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_INVITE_CODE",
    "REPORTS_PUBLIC_BASE_URL",
    "REPORTS_SIGNING_KEY",
    "BOT_PRESET",
    "BOT_DAILY_CAP",
    "REPORTS_WEB_HOST",
    "REPORTS_WEB_PORT",
    "BOT_ADMIN_USER_ID",
    "BOT_DB_PATH",
)


def _clear_service_env(monkeypatch):
    for key in _SERVICE_ENV_VARS:
        monkeypatch.delenv(key, raising=False)


def _set_required_env(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_INVITE_CODE", "test-invite")
    monkeypatch.setenv("REPORTS_PUBLIC_BASE_URL", "https://example.invalid")
    monkeypatch.setenv("REPORTS_SIGNING_KEY", "test-signing-key")


def test_from_env_parses_defaults_with_only_required_vars_set(monkeypatch):
    # Arrange
    _clear_service_env(monkeypatch)
    _set_required_env(monkeypatch)

    # Act
    config = ServiceConfig.from_env()

    # Assert
    assert config.bot_token == "test-token"
    assert config.invite_code == "test-invite"
    assert config.public_base_url == "https://example.invalid"
    assert config.reports_signing_key == "test-signing-key"
    assert config.daily_cap == DEFAULT_DAILY_CAP
    assert config.preset == DEFAULT_PRESET
    assert config.web_host == DEFAULT_WEB_HOST
    assert config.web_port == DEFAULT_WEB_PORT
    assert config.db_path == DEFAULT_DB_PATH
    assert config.admin_user_id is None


def test_from_env_strips_trailing_slash_from_public_base_url(monkeypatch):
    # Arrange
    _clear_service_env(monkeypatch)
    _set_required_env(monkeypatch)
    monkeypatch.setenv("REPORTS_PUBLIC_BASE_URL", "https://example.invalid/")

    # Act
    config = ServiceConfig.from_env()

    # Assert
    assert config.public_base_url == "https://example.invalid"


def test_from_env_raises_config_error_listing_all_missing_required_vars(monkeypatch):
    # Arrange
    _clear_service_env(monkeypatch)

    # Act
    with pytest.raises(ConfigError) as exc_info:
        ServiceConfig.from_env()

    # Assert
    message = str(exc_info.value)
    assert "TELEGRAM_BOT_TOKEN" in message
    assert "TELEGRAM_INVITE_CODE" in message
    assert "REPORTS_PUBLIC_BASE_URL" in message
    assert "REPORTS_SIGNING_KEY" in message


def test_from_env_raises_config_error_for_unknown_preset(monkeypatch):
    # Arrange
    _clear_service_env(monkeypatch)
    _set_required_env(monkeypatch)
    monkeypatch.setenv("BOT_PRESET", "not-a-real-preset")

    # Act
    with pytest.raises(ConfigError) as exc_info:
        ServiceConfig.from_env()

    # Assert
    message = str(exc_info.value)
    assert "not-a-real-preset" in message
    assert "cost_saver" in message
    assert "standard" in message


def test_from_env_raises_config_error_for_non_integer_daily_cap(monkeypatch):
    # Arrange
    _clear_service_env(monkeypatch)
    _set_required_env(monkeypatch)
    monkeypatch.setenv("BOT_DAILY_CAP", "not-a-number")

    # Act
    with pytest.raises(ConfigError) as exc_info:
        ServiceConfig.from_env()

    # Assert
    assert "BOT_DAILY_CAP" in str(exc_info.value)


def test_from_env_raises_config_error_for_zero_daily_cap(monkeypatch):
    # Arrange
    _clear_service_env(monkeypatch)
    _set_required_env(monkeypatch)
    monkeypatch.setenv("BOT_DAILY_CAP", "0")

    # Act
    with pytest.raises(ConfigError) as exc_info:
        ServiceConfig.from_env()

    # Assert
    assert "BOT_DAILY_CAP" in str(exc_info.value)


def test_from_env_raises_config_error_for_invalid_web_port(monkeypatch):
    # Arrange
    _clear_service_env(monkeypatch)
    _set_required_env(monkeypatch)
    monkeypatch.setenv("REPORTS_WEB_PORT", "70000")

    # Act
    with pytest.raises(ConfigError) as exc_info:
        ServiceConfig.from_env()

    # Assert
    assert "REPORTS_WEB_PORT" in str(exc_info.value)


def test_from_env_uses_default_web_host_for_blank_override(monkeypatch):
    # Arrange
    _clear_service_env(monkeypatch)
    _set_required_env(monkeypatch)
    monkeypatch.setenv("REPORTS_WEB_HOST", "   ")

    # Act
    config = ServiceConfig.from_env()

    # Assert
    assert config.web_host == DEFAULT_WEB_HOST


def test_from_env_raises_config_error_for_non_integer_admin_user_id(monkeypatch):
    # Arrange
    _clear_service_env(monkeypatch)
    _set_required_env(monkeypatch)
    monkeypatch.setenv("BOT_ADMIN_USER_ID", "not-a-number")

    # Act
    with pytest.raises(ConfigError) as exc_info:
        ServiceConfig.from_env()

    # Assert
    assert "BOT_ADMIN_USER_ID" in str(exc_info.value)


def test_from_env_parses_optional_overrides(monkeypatch):
    # Arrange
    _clear_service_env(monkeypatch)
    _set_required_env(monkeypatch)
    monkeypatch.setenv("BOT_PRESET", "standard")
    monkeypatch.setenv("BOT_DAILY_CAP", "10")
    monkeypatch.setenv("REPORTS_WEB_HOST", "0.0.0.0")
    monkeypatch.setenv("REPORTS_WEB_PORT", "9000")
    monkeypatch.setenv("BOT_ADMIN_USER_ID", "12345")
    monkeypatch.setenv("BOT_DB_PATH", "/tmp/custom-service.db")

    # Act
    config = ServiceConfig.from_env()

    # Assert
    assert config.preset == "standard"
    assert config.daily_cap == 10
    assert config.web_host == "0.0.0.0"
    assert config.web_port == 9000
    assert config.admin_user_id == 12345
    assert config.db_path == Path("/tmp/custom-service.db")
