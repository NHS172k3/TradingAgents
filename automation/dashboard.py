"""Streamlit dashboard for the TradingAgents automation layer.

    streamlit run automation/dashboard.py

Read-only over the three existing data sources (decisions.jsonl, the
upstream memory log, report files on disk) — except the Watchlist page,
which round-trips watchlist.yaml through the validated saver.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import subprocess
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

# Streamlit puts this script's directory on sys.path, not the repo root, so
# `import automation` fails unless launched from the repo root. Self-locate.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

try:  # secrets live in the repo-root .env, same as the runner/notifiers
    from dotenv import load_dotenv, find_dotenv
    load_dotenv(_REPO_ROOT / ".env")
except ImportError:
    pass

from automation import settings, upstream
from automation.calendar_check import is_trading_day, next_trading_day
from automation.settings import TickerSpec

RATING_ORDER = ["Buy", "Overweight", "Hold", "Underweight", "Sell"]
RATING_COLORS = {
    "Buy": "#1a9850", "Overweight": "#91cf60", "Hold": "#999999",
    "Underweight": "#fc8d59", "Sell": "#d73027",
}

# Report section tabs -> subfolders written by cli.main.save_report_to_disk
REPORT_SECTIONS = [
    ("Analysts", "1_analysts"),
    ("Research", "2_research"),
    ("Trading", "3_trading"),
    ("Risk", "4_risk"),
    ("Portfolio decision", "5_portfolio"),
]


def _pct_to_float(value: str | None) -> float | None:
    """'+3.2%' -> 0.032; tolerate None/'n/a'."""
    if not value or "%" not in str(value):
        return None
    try:
        return float(str(value).strip().rstrip("%")) / 100.0
    except ValueError:
        return None


@st.cache_data(ttl=60)
def load_decisions() -> pd.DataFrame:
    rows = []
    if settings.DECISIONS_PATH.exists():
        for line in settings.DECISIONS_PATH.read_text(encoding="utf-8").splitlines():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    df = pd.DataFrame(rows)
    if not df.empty:
        df["recorded_at"] = pd.to_datetime(df["recorded_at"], errors="coerce")
    return df


@st.cache_data(ttl=60)
def load_memory() -> pd.DataFrame:
    df = pd.DataFrame(upstream.read_memory_entries())
    if not df.empty:
        df["alpha_f"] = df["alpha"].map(_pct_to_float)
        df["raw_f"] = df["raw"].map(_pct_to_float)
    return df


def style_ratings(df: pd.DataFrame, column: str):
    def color(rating):
        c = RATING_COLORS.get(rating)
        return f"color: {c}; font-weight: bold" if c else ""
    return df.style.map(color, subset=[column])


def page_overview(watchlist, decisions: pd.DataFrame, memory: pd.DataFrame) -> None:
    st.header("Overview")

    today = _dt.date.today().isoformat()
    c1, c2, c3 = st.columns(3)
    if decisions.empty:
        c1.metric("Last run", "never")
    else:
        last_ts = decisions["recorded_at"].max()
        last_run = decisions[decisions["recorded_at"] == last_ts]
        last_day = decisions[decisions["recorded_at"].dt.date == last_ts.date()]
        ok = int(last_day["error"].isna().sum())
        failed = int(last_day["error"].notna().sum())
        c1.metric("Last run", str(last_ts.date()))
        c2.metric("Last run results", f"{ok} ok / {failed} failed")
    upcoming = today if is_trading_day(today, watchlist.skip_dates) else \
        next_trading_day(today, watchlist.skip_dates)
    c3.metric("Next trading day", upcoming)

    rows = []
    for spec in watchlist.tickers:
        latest = decisions[decisions["ticker"] == spec.symbol].sort_values(
            "recorded_at").tail(1) if not decisions.empty else pd.DataFrame()
        mem = memory[memory["ticker"] == spec.symbol] if not memory.empty else pd.DataFrame()
        pending = int(mem["pending"].sum()) if not mem.empty else 0
        row = {"ticker": spec.symbol, "preset": spec.preset, "rating": None,
               "date": None, "duration (s)": None, "pending outcomes": pending,
               "last error": None}
        if not latest.empty:
            rec = latest.iloc[0]
            row.update({"rating": rec.get("rating"), "date": rec.get("date"),
                        "duration (s)": rec.get("duration_seconds"),
                        "last error": rec.get("error")})
        rows.append(row)
    table = pd.DataFrame(rows)
    st.dataframe(style_ratings(table, "rating"), width="stretch",
                 hide_index=True)
    if decisions.empty:
        st.info("No runs recorded yet — run "
                "`python -m automation.run_watchlist` to populate this page.")


def page_performance(memory: pd.DataFrame) -> None:
    st.header("Performance")
    if memory.empty:
        st.info("Memory log is empty — outcomes appear after a decision is "
                "resolved by a later run of the same ticker.")
        return

    resolved = memory[~memory["pending"]].copy()
    pending = memory[memory["pending"]]

    if resolved.empty:
        st.info("No resolved outcomes yet (all decisions still pending).")
    else:
        with_alpha = resolved.dropna(subset=["alpha_f"])
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Resolved decisions", len(resolved))
        if not with_alpha.empty:
            hit = (with_alpha["alpha_f"] > 0).mean()
            c2.metric("Hit rate (alpha > 0)", f"{hit:.0%}")
            c3.metric("Avg alpha", f"{with_alpha['alpha_f'].mean():+.1%}")
        raw = resolved.dropna(subset=["raw_f"])
        if not raw.empty:
            c4.metric("Avg raw return", f"{raw['raw_f'].mean():+.1%}")

        st.subheader("Per ticker")
        per = (resolved.dropna(subset=["alpha_f"])
               .groupby("ticker")
               .agg(decisions=("alpha_f", "size"),
                    hit_rate=("alpha_f", lambda s: (s > 0).mean()),
                    avg_alpha=("alpha_f", "mean"),
                    avg_raw=("raw_f", "mean")))
        if not per.empty:
            st.dataframe(per.style.format({"hit_rate": "{:.0%}",
                                           "avg_alpha": "{:+.1%}",
                                           "avg_raw": "{:+.1%}"}),
                         width="stretch")

        st.subheader("Rating distribution (resolved)")
        counts = (resolved["rating"].value_counts()
                  .reindex(RATING_ORDER).fillna(0).astype(int))
        st.bar_chart(counts)

        cum = resolved.dropna(subset=["alpha_f"]).sort_values("date")
        if len(cum) > 1:
            st.subheader("Cumulative alpha over time")
            chart = cum.set_index("date")["alpha_f"].cumsum()
            st.line_chart(chart)

    if not pending.empty:
        st.subheader(f"Awaiting resolution ({len(pending)})")
        st.dataframe(pending[["date", "ticker", "rating"]],
                     width="stretch", hide_index=True)
        st.caption("Pending outcomes resolve automatically on the next run "
                   "of the same ticker.")


def page_history(decisions: pd.DataFrame, memory: pd.DataFrame) -> None:
    st.header("History")
    if memory.empty and decisions.empty:
        st.info("Nothing recorded yet.")
        return

    base = memory if not memory.empty else pd.DataFrame(
        columns=["date", "ticker", "rating", "pending", "raw", "alpha",
                 "holding", "decision", "reflection"])

    tickers = sorted(set(base["ticker"]) | (set(decisions["ticker"])
                     if not decisions.empty else set()))
    f1, f2, f3 = st.columns(3)
    sel_tickers = f1.multiselect("Tickers", tickers, default=tickers)
    sel_ratings = f2.multiselect("Ratings", RATING_ORDER, default=RATING_ORDER)
    sel_status = f3.radio("Status", ["all", "pending", "resolved"], horizontal=True)

    view = base[base["ticker"].isin(sel_tickers) & base["rating"].isin(sel_ratings)]
    if sel_status == "pending":
        view = view[view["pending"]]
    elif sel_status == "resolved":
        view = view[~view["pending"]]
    view = view.sort_values("date", ascending=False)

    # decisions.jsonl adds duration + report path for runs made by this layer
    extra = {}
    if not decisions.empty:
        for _, rec in decisions.iterrows():
            extra[(rec["ticker"], rec["date"])] = rec

    st.caption(f"{len(view)} entries")
    for _, e in view.iterrows():
        status = "pending" if e["pending"] else \
            f"raw {e['raw']} | alpha {e['alpha']} | {e['holding']}"
        with st.expander(f"{e['date']}  {e['ticker']} — {e['rating']}  ({status})"):
            rec = extra.get((e["ticker"], e["date"]))
            if rec is not None:
                meta = f"preset {rec.get('preset')} · {rec.get('duration_seconds')}s"
                if rec.get("report_dir"):
                    meta += f" · `{rec['report_dir']}`"
                st.caption(meta)
            if e.get("decision"):
                st.markdown("**Decision**")
                st.markdown(e["decision"])
            if e.get("reflection"):
                st.markdown("**Reflection**")
                st.markdown(e["reflection"])


def page_reports() -> None:
    st.header("Reports")
    root = upstream.results_dir()
    tickers = sorted(p.name for p in root.iterdir() if p.is_dir()) \
        if root.exists() else []
    if not tickers:
        st.info(f"No results found under `{root}`.")
        return

    ticker = st.selectbox("Ticker", tickers)
    dates = sorted((p.name for p in (root / ticker).iterdir()
                    if p.is_dir() and p.name != "TradingAgentsStrategy_logs"),
                   reverse=True)
    if dates:
        date = st.selectbox("Date", dates)
        report_dir = root / ticker / date / "reports"
        complete = report_dir / "complete_report.md"
        tabs = st.tabs([name for name, _ in REPORT_SECTIONS] + ["Full report"])
        for tab, (_, folder) in zip(tabs, REPORT_SECTIONS):
            with tab:
                section_dir = report_dir / folder
                files = sorted(section_dir.glob("*.md")) if section_dir.exists() else []
                if not files:
                    st.caption("No content for this section.")
                for f in files:
                    st.subheader(f.stem.replace("_", " ").title())
                    st.markdown(f.read_text(encoding="utf-8"))
        with tabs[-1]:
            if complete.exists():
                st.markdown(complete.read_text(encoding="utf-8"))
            else:
                st.caption("complete_report.md not found for this run.")
        return

    # Older runs (pre-automation) only have the raw state JSON
    st.caption("No report folders for this ticker — falling back to raw state logs.")
    logs = sorted((root / ticker / "TradingAgentsStrategy_logs").glob(
        "full_states_log_*.json"), reverse=True)
    if not logs:
        st.info("No state logs either.")
        return
    chosen = st.selectbox("State log", [p.name for p in logs])
    state = json.loads((root / ticker / "TradingAgentsStrategy_logs" / chosen)
                       .read_text(encoding="utf-8"))
    for key in ("final_trade_decision", "investment_plan", "market_report",
                "sentiment_report", "news_report", "fundamentals_report"):
        if state.get(key):
            st.subheader(key.replace("_", " ").title())
            st.markdown(state[key])


def page_watchlist(watchlist) -> None:
    st.header("Watchlist")
    st.caption(f"Default preset: **{watchlist.preset}** — edits are validated "
               "and written back to `automation/watchlist.yaml`.")
    df = pd.DataFrame([{"symbol": t.symbol, "preset": t.preset,
                        "asset_type": t.asset_type} for t in watchlist.tickers])
    edited = st.data_editor(
        df, num_rows="dynamic", width="stretch", hide_index=True,
        column_config={
            "preset": st.column_config.SelectboxColumn(
                options=sorted(settings.PRESETS), required=True),
            "asset_type": st.column_config.SelectboxColumn(
                options=list(settings.VALID_ASSET_TYPES), required=True),
        })
    if st.button("Save watchlist"):
        specs = [TickerSpec(symbol=str(r["symbol"]).strip().upper(),
                            preset=r["preset"], asset_type=r["asset_type"])
                 for _, r in edited.iterrows() if str(r["symbol"]).strip()]
        try:
            settings.save_watchlist_tickers(specs)
            st.success(f"Saved {len(specs)} tickers.")
            st.cache_data.clear()
        except settings.WatchlistError as exc:
            st.error(str(exc))


REQUIRED_ENV_VARS = ["OPENAI_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
                     "GMAIL_ADDRESS", "GMAIL_APP_PASSWORD"]
SCHEDULED_TASKS = ["TradingAgents Daily", "TradingAgents Weekly Email"]

WORKFLOW_DOC = """
### How it works

```
Mon-Fri 17:30  Task Scheduler -> run_watchlist -> one TradingAgents run per ticker
                 -> reports on disk + decisions.jsonl + Telegram message per ticker
Sun 18:00      Task Scheduler -> weekly_email -> Gmail digest of the week
Anytime        this dashboard (read-only over the same files; no LLM calls)
```

Outcomes are tracked automatically: each run resolves the previous pending
decision for the same ticker, which feeds the **Performance** page.

**Timing (from UTC+8):** the 17:30 local run analyzes data through the most
recent US close (04:00/05:00 local) and finishes ~4h before the next US open
(21:30/22:30 local) — so each alert arrives in time to act at the open.
Monday's run still sees only Friday's close (no weekend sessions).

### Day-to-day workflow

1. **Evenings (Mon-Fri, after the 17:30 run)** — a Telegram message arrives per
   ticker with its rating. No action needed for the run itself.
2. **Review** — open this dashboard (`streamlit run automation/dashboard.py`).
   **Overview** shows the latest run; **Reports** has the full per-analyst
   reasoning behind any rating you want to sanity-check.
3. **Decide** — ratings are research input, not orders. If you act on one,
   note it; the system tracks decision quality (alpha vs. holding) on the
   **Performance** page either way.
4. **Sunday evening** — the weekly digest email summarizes the week's
   decisions and resolved outcomes.
5. **Maintain** — add/remove tickers on the **Watchlist** page (validated and
   written back to `watchlist.yaml`). Use `skip_dates` in the yaml for ad-hoc
   market closures.

### Manual commands

| Command | Purpose |
|---|---|
| `python -m automation.run_watchlist --dry-run` | validate config, no LLM calls |
| `python -m automation.run_watchlist` | full watchlist run now |
| `python -m automation.run_watchlist --ticker NVDA --preset cost_saver` | one ticker |
| `python -m automation.notify_telegram` | send a test Telegram message |
| `python -m automation.weekly_email --dry-run` | print the digest without sending |
| `schtasks /Run /TN "TradingAgents Daily"` | trigger the scheduled task now |

### Costs & runtime (local models)

LLM calls run locally on Ollama (`qwen3:8b`, set in `.env`) — **$0 per run**.
The cost is time instead: expect roughly 5-15 min/ticker on an 8 GB GPU vs.
~1-3 min on cloud models. The scheduled task's 6 h limit covers the watchlist
comfortably. Presets still matter: `standard` = 4 analysts, full debate
(~12 calls/ticker); `cost_saver` = market+news, shorter debates (~8 calls).

**Reverting to OpenAI:** comment out the three `TRADINGAGENTS_*` lines in the
repo-root `.env`. Provider/model are env-driven; presets never pin models.

### Troubleshooting

- **Task log says exit 2** — Python not found: set the `TRADINGAGENTS_PYTHON`
  user env var (or create `.venv`), then re-register tasks.
- **Run logs** — `automation/logs/run_YYYY-MM-DD.log` and `task_*.log`.
- **Runs fail with connection errors** — Ollama isn't running: start the
  Ollama app/service (check the status panel above).
- **Analyses look truncated or shallow** — context window too small: set the
  `OLLAMA_CONTEXT_LENGTH` user env var (16384) and restart Ollama.
- **Gmail auth fails** — app password must be valid; if pasted with spaces and
  login fails, remove the spaces in `.env`.
- **No Telegram message** — check `telegram.enabled` in `watchlist.yaml`, then
  `python -m automation.notify_telegram`.
"""


def _ollama_reachable() -> bool:
    import requests
    try:
        return requests.get("http://localhost:11434/api/version",
                            timeout=2).ok
    except requests.RequestException:
        return False


def _task_registered(name: str) -> bool:
    if sys.platform != "win32":
        return False
    result = subprocess.run(["schtasks", "/query", "/tn", name],
                            capture_output=True, timeout=10)
    return result.returncode == 0


def page_workflow() -> None:
    st.header("Workflow")

    st.subheader("Setup status")
    provider = os.environ.get("TRADINGAGENTS_LLM_PROVIDER", "openai").lower()
    is_local = provider == "ollama"

    c1, c2, c3 = st.columns(3)
    with c1:
        st.markdown("**Secrets (.env)** — presence only, values never shown")
        for var in REQUIRED_ENV_VARS:
            ok = bool(os.environ.get(var, "").strip())
            if var == "OPENAI_API_KEY" and is_local:
                st.markdown(f"➖ `{var}` — not required (local provider)")
            else:
                st.markdown(f"{'✅' if ok else '❌'} `{var}`")
    with c2:
        st.markdown("**LLM backend**")
        model = os.environ.get("TRADINGAGENTS_DEEP_THINK_LLM", "(default)")
        st.markdown(f"Provider: `{provider}` · model: `{model}`")
        if is_local:
            ok = _ollama_reachable()
            st.markdown(f"{'✅' if ok else '❌'} Ollama at `localhost:11434`")
            if not ok:
                st.caption("Start the Ollama app/service, then refresh.")
    with c3:
        st.markdown("**Scheduled tasks**")
        for task in SCHEDULED_TASKS:
            ok = _task_registered(task)
            st.markdown(f"{'✅' if ok else '❌'} {task}")
        if not all(_task_registered(t) for t in SCHEDULED_TASKS):
            st.caption("Register with: `powershell -ExecutionPolicy Bypass "
                       "-File automation\\windows\\register_tasks.ps1`")

    st.markdown(WORKFLOW_DOC)


def main() -> None:
    st.set_page_config(page_title="TradingAgents", layout="wide")
    st.sidebar.title("TradingAgents")
    page = st.sidebar.radio(
        "Page", ["Overview", "Performance", "History", "Reports", "Watchlist",
                 "Workflow"])
    if st.sidebar.button("Refresh data"):
        st.cache_data.clear()

    try:
        watchlist = settings.load_watchlist()
    except settings.WatchlistError as exc:
        st.error(f"watchlist.yaml is invalid: {exc}")
        return

    decisions = load_decisions()
    memory = load_memory()

    if page == "Overview":
        page_overview(watchlist, decisions, memory)
    elif page == "Performance":
        page_performance(memory)
    elif page == "History":
        page_history(decisions, memory)
    elif page == "Reports":
        page_reports()
    elif page == "Watchlist":
        page_watchlist(watchlist)
    else:
        page_workflow()


main()
