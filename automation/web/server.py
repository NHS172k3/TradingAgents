"""FastAPI app that hosts pre-rendered analysis reports.

Bound to ``127.0.0.1`` only — Cloudflare Tunnel is the sole public ingress
(see ``automation/linux/cloudflared.service``). Reports are rendered once by
:func:`automation.reports.render_to_html` and served here as static files;
this module does no per-request markdown work.

Report tokens are verified, not looked up (see :mod:`automation.tokens`) —
there is no bearer secret in the database to leak. Requests are rate
limited per client (keyed off Cloudflare's ``CF-Connecting-IP`` header,
falling back to the raw socket address) to slow down scraping/abuse.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from automation.config import ServiceConfig
from automation.runlog import get_logger
from automation.store import Store
from automation.tokens import verify_report_token

log = get_logger(__name__)

REPORT_CACHE_CONTROL = "private, max-age=3600"
REPORT_RATE_LIMIT = "30/minute"


def _client_key(request: Request) -> str:
    """Prefer Cloudflare's real-visitor-IP header; Cloudflare Tunnel proxies
    every request through a local loopback connection, so the raw socket
    address (``get_remote_address``) would otherwise be the same for every
    visitor and rate-limit them all as one shared bucket."""
    return request.headers.get("cf-connecting-ip") or get_remote_address(request)


def create_app(store: Store, config: ServiceConfig) -> FastAPI:
    """Build the FastAPI app, injecting ``store``/``config`` for report
    lookups and token verification."""
    limiter = Limiter(key_func=_client_key)
    app = FastAPI(title="TradingAgents Reports")
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True}

    @app.get("/report/{report_id}")
    @limiter.limit(REPORT_RATE_LIMIT)
    def get_report(request: Request, report_id: str, token: str = "") -> FileResponse:
        report = store.get_report(report_id)
        if report is None:
            raise HTTPException(status_code=404, detail="Report not found")

        signed_user_id = verify_report_token(config.reports_signing_key, token, report_id)
        if signed_user_id is None or signed_user_id != report.user_id:
            raise HTTPException(status_code=403, detail="Invalid, expired, or missing token")

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
    uvicorn.run(create_app(store, cfg), host=cfg.web_host, port=cfg.web_port)
