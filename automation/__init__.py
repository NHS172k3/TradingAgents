"""Local automation layer for TradingAgents.

Scheduled watchlist runs, Telegram alerts, weekly email digest, and a
Streamlit dashboard. Lives entirely outside the upstream packages
(``tradingagents``/``cli``) so the repo can keep tracking upstream;
all upstream imports are confined to :mod:`automation.upstream`.
"""
