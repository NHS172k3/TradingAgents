"""The only automation module that imports tradingagents/cli internals.

Everything else goes through this facade so upstream refactors are a
one-file fix. All upstream imports are lazy (inside functions) so that
``--dry-run``, the dashboard, and the weekly email never load the LLM
stack just to read config or parse the memory log.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from automation.settings import PRESETS

_graph_cache: dict[str, object] = {}


def _default_config() -> dict:
    from tradingagents.default_config import DEFAULT_CONFIG
    return DEFAULT_CONFIG


def results_dir() -> Path:
    return Path(_default_config()["results_dir"]).expanduser()


def memory_log_path() -> Path:
    return Path(_default_config()["memory_log_path"]).expanduser()


def build_graph(preset_name: str):
    """Build (or fetch cached) TradingAgentsGraph for a preset."""
    if preset_name in _graph_cache:
        return _graph_cache[preset_name]
    from tradingagents.graph.trading_graph import TradingAgentsGraph

    preset = PRESETS[preset_name]
    config = _default_config().copy()
    config.update(preset["config_patch"])
    graph = TradingAgentsGraph(
        selected_analysts=preset["selected_analysts"],
        config=config,
        debug=False,
    )
    _graph_cache[preset_name] = graph
    return graph


def save_reports(final_state: dict, ticker: str, date: str) -> Path:
    """Write per-section markdown + complete_report.md, CLI-compatible.

    Reuses ``cli.main.save_report_to_disk`` (verified import-safe). Falls
    back to dumping the final decision alone so report-writing breakage
    never fails a run that already paid for its LLM calls.
    """
    report_dir = results_dir() / ticker / date / "reports"
    try:
        from cli.main import save_report_to_disk
        save_report_to_disk(final_state, ticker, report_dir)
    except Exception:
        report_dir.mkdir(parents=True, exist_ok=True)
        decision = final_state.get("final_trade_decision", "")
        (report_dir / "decision.md").write_text(str(decision), encoding="utf-8")
    return report_dir


# Fallback parser for the memory log's tag lines:
#   [2026-06-10 | NVDA | Buy | pending]
#   [2026-06-10 | NVDA | Buy | +3.2% | +1.1% | 7d]
_TAG_RE = re.compile(r"^\[([\d-]+) \| ([^|]+) \| ([^|]+?)(?: \| ([^\]]+))?\]$")


def _read_memory_entries_fallback() -> list[dict]:
    path = memory_log_path()
    if not path.exists():
        return []
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        m = _TAG_RE.match(line.strip())
        if not m:
            continue
        date, ticker, rating, rest = m.groups()
        fields = [f.strip() for f in (rest or "").split("|")]
        pending = fields[:1] == ["pending"]
        entries.append({
            "date": date.strip(),
            "ticker": ticker.strip(),
            "rating": rating.strip(),
            "pending": pending,
            "raw": fields[0] if fields and not pending else None,
            "alpha": fields[1] if len(fields) > 1 else None,
            "holding": fields[2] if len(fields) > 2 else None,
            "decision": "",
            "reflection": "",
        })
    return entries


def read_memory_entries() -> list[dict]:
    """Parsed memory-log entries: date, ticker, rating, pending, raw, alpha,
    holding, decision, reflection. Prefers the upstream parser; regex
    fallback if upstream's interface drifts."""
    try:
        from tradingagents.agents.utils.memory import TradingMemoryLog
        log = TradingMemoryLog({"memory_log_path": str(memory_log_path())})
        return log.load_entries()
    except Exception:
        return _read_memory_entries_fallback()


def propagate(graph, symbol: str, date: str, asset_type: str = "stock"):
    """Run one analysis. Returns (final_state, rating) where rating is one of
    Buy / Overweight / Hold / Underweight / Sell."""
    return graph.propagate(symbol, date, asset_type=asset_type)


def full_states_log_path(ticker: str, date: str) -> Optional[Path]:
    """Path of the raw state JSON propagate() writes, if present."""
    p = results_dir() / ticker / "TradingAgentsStrategy_logs" / f"full_states_log_{date}.json"
    return p if p.exists() else None
