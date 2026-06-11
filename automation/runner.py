"""Run one watchlist ticker through the TradingAgents graph."""

from __future__ import annotations

import time
from dataclasses import dataclass, asdict
from typing import Optional

from automation import upstream
from automation.settings import TickerSpec

RATIONALE_EXCERPT_CHARS = 400


@dataclass
class RunResult:
    ticker: str
    date: str
    preset: str
    rating: Optional[str] = None       # Buy/Overweight/Hold/Underweight/Sell
    rationale: str = ""                # excerpt of final_trade_decision
    report_dir: Optional[str] = None
    duration_seconds: float = 0.0
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.error is None

    def to_record(self) -> dict:
        d = asdict(self)
        d.pop("rationale", None)  # full text lives in the reports/memory log
        return d


def run_one_ticker(spec: TickerSpec, date: str) -> RunResult:
    """Run the full pipeline for one ticker. Never raises; errors land in
    ``RunResult.error`` so the watchlist loop can continue."""
    start = time.monotonic()
    result = RunResult(ticker=spec.symbol, date=date, preset=spec.preset)
    try:
        graph = upstream.build_graph(spec.preset)
        final_state, rating = upstream.propagate(
            graph, spec.symbol, date, asset_type=spec.asset_type
        )
        report_dir = upstream.save_reports(final_state, spec.symbol, date)
        result.rating = rating
        result.rationale = str(final_state.get("final_trade_decision", ""))[
            :RATIONALE_EXCERPT_CHARS
        ]
        result.report_dir = str(report_dir)
    except Exception as exc:  # noqa: BLE001 — per-ticker isolation is the point
        result.error = f"{type(exc).__name__}: {exc}"
    result.duration_seconds = round(time.monotonic() - start, 1)
    return result
