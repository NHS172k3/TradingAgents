"""Watchlist config loading/validation and run presets.

This module is intentionally light: no ``tradingagents`` imports, so the
dry-run path and the dashboard can load configuration without pulling in
the LLM stack. Preset config patches are applied over the upstream
``DEFAULT_CONFIG`` inside :mod:`automation.upstream`.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

AUTOMATION_DIR = Path(__file__).resolve().parent
REPO_ROOT = AUTOMATION_DIR.parent
WATCHLIST_PATH = AUTOMATION_DIR / "watchlist.yaml"
LOGS_DIR = AUTOMATION_DIR / "logs"
DECISIONS_PATH = LOGS_DIR / "decisions.jsonl"
LOCKFILE_PATH = LOGS_DIR / ".run.lock"

VALID_ASSET_TYPES = ("stock", "crypto")

# preset name -> (selected_analysts, config patch over DEFAULT_CONFIG)
#
# Presets control analyst count and debate depth only. Provider and model
# selection lives in .env via the upstream TRADINGAGENTS_* env overrides
# (currently ollama/qwen3:8b), so presets must NOT pin model names — a
# config_patch entry would silently override the env-configured provider.
PRESETS: dict[str, dict] = {
    "standard": {
        "selected_analysts": ["market", "social", "news", "fundamentals"],
        "config_patch": {},
    },
    "cost_saver": {
        "selected_analysts": ["market", "news"],
        "config_patch": {
            "max_debate_rounds": 1,
            "max_risk_discuss_rounds": 1,
            "news_article_limit": 10,
        },
    },
}


class WatchlistError(ValueError):
    """Raised when watchlist.yaml is missing or invalid."""


@dataclass
class TickerSpec:
    symbol: str
    preset: str
    asset_type: str = "stock"


@dataclass
class Watchlist:
    preset: str
    tickers: list[TickerSpec]
    skip_dates: list[str] = field(default_factory=list)
    telegram_enabled: bool = True
    telegram_notify_on_failure: bool = True
    email_enabled: bool = True
    email_to: Optional[str] = None
    continue_on_error: bool = True
    max_consecutive_failures: int = 3


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise WatchlistError(f"watchlist.yaml: {message}")


def load_watchlist(path: Path = WATCHLIST_PATH) -> Watchlist:
    _require(path.exists(), f"not found at {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    _require(isinstance(raw, dict), "top level must be a mapping")

    default_preset = raw.get("preset", "standard")
    _require(default_preset in PRESETS,
             f"unknown preset {default_preset!r} (valid: {', '.join(PRESETS)})")

    raw_tickers = raw.get("tickers") or []
    _require(isinstance(raw_tickers, list) and raw_tickers,
             "'tickers' must be a non-empty list")
    tickers = []
    for item in raw_tickers:
        if isinstance(item, str):
            item = {"symbol": item}
        _require(isinstance(item, dict) and item.get("symbol"),
                 f"each ticker needs a 'symbol' (got {item!r})")
        symbol = str(item["symbol"]).strip().upper()
        preset = item.get("preset", default_preset)
        _require(preset in PRESETS,
                 f"ticker {symbol}: unknown preset {preset!r}")
        asset_type = item.get("asset_type", "stock")
        _require(asset_type in VALID_ASSET_TYPES,
                 f"ticker {symbol}: asset_type must be one of {VALID_ASSET_TYPES}")
        tickers.append(TickerSpec(symbol=symbol, preset=preset, asset_type=asset_type))

    skip_dates = raw.get("skip_dates") or []
    _require(isinstance(skip_dates, list), "'skip_dates' must be a list")
    for d in skip_dates:
        try:
            _dt.date.fromisoformat(str(d))
        except ValueError:
            raise WatchlistError(f"watchlist.yaml: skip_dates entry {d!r} is not YYYY-MM-DD")

    telegram = raw.get("telegram") or {}
    email = raw.get("email") or {}
    run = raw.get("run") or {}

    return Watchlist(
        preset=default_preset,
        tickers=tickers,
        skip_dates=[str(d) for d in skip_dates],
        telegram_enabled=bool(telegram.get("enabled", True)),
        telegram_notify_on_failure=bool(telegram.get("notify_on_failure", True)),
        email_enabled=bool(email.get("enabled", True)),
        email_to=email.get("to"),
        continue_on_error=bool(run.get("continue_on_error", True)),
        max_consecutive_failures=int(run.get("max_consecutive_failures", 3)),
    )


def save_watchlist_tickers(tickers: list[TickerSpec], path: Path = WATCHLIST_PATH) -> None:
    """Round-trip the tickers section of watchlist.yaml (used by the dashboard).

    Re-validates by reloading after the write; restores the previous content
    if the result fails validation.
    """
    original = path.read_text(encoding="utf-8")
    raw = yaml.safe_load(original)
    raw["tickers"] = [
        {"symbol": t.symbol, **({"preset": t.preset} if t.preset != raw.get("preset") else {}),
         **({"asset_type": t.asset_type} if t.asset_type != "stock" else {})}
        for t in tickers
    ]
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    try:
        load_watchlist(path)
    except WatchlistError:
        path.write_text(original, encoding="utf-8")
        raise
