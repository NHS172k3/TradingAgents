"""FastAPI app that hosts pre-rendered analysis reports.

Bound to ``127.0.0.1`` only — Cloudflare Tunnel is the sole public ingress
(see ``automation/linux/cloudflared.service``). Reports are rendered once by
:func:`automation.reports.render_to_html` and served here as static files;
this module does no per-request markdown work.
"""

from __future__ import annotations

import hmac
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

from automation.config import ServiceConfig
from automation.runlog import get_logger
from automation.store import Store

log = get_logger(__name__)

REPORT_CACHE_CONTROL = "private, max-age=3600"


def create_app(store: Store) -> FastAPI:
    """Build the FastAPI app, injecting ``store`` for report/token lookups."""
    app = FastAPI(title="TradingAgents Reports")

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True}

    @app.get("/report/{report_id}")
    def get_report(report_id: str, token: str = "") -> FileResponse:
        report = store.get_report(report_id)
        if report is None:
            raise HTTPException(status_code=404, detail="Report not found")

        user_token = store.get_token(report.user_id)
        if not user_token or not hmac.compare_digest(token, user_token):
            raise HTTPException(status_code=403, detail="Invalid or missing token")

        html_path = Path(report.html_path)
        if not html_path.exists():
            raise HTTPException(status_code=410, detail="Report file no longer available")

        return FileResponse(
            html_path,
            media_type="text/html",
            headers={"Cache-Control": REPORT_CACHE_CONTROL},
        )

    return app


if __name__ == "__main__":
    import uvicorn

    cfg = ServiceConfig.from_env()
    store = Store(cfg.db_path)
    store.init_db()
    uvicorn.run(create_app(store), host=cfg.web_host, port=cfg.web_port)
