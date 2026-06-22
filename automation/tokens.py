"""Stateless, signed tokens for report links.

Report links carry no stored bearer secret. The token is an HMAC
signature (via ``itsdangerous``) over ``(user_id, report_id)``, verified
by recomputing the signature with one server-wide secret key
(``REPORTS_SIGNING_KEY``). There is nothing to look up or leak from the
database; rotating the secret key invalidates every outstanding link.
"""

from __future__ import annotations

import datetime as _dt
from typing import Optional

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

REPORT_TOKEN_MAX_AGE_SECONDS = 7 * 24 * 3600  # 7 days
_SALT = "report-token"


def sign_report_token(secret_key: str, user_id: int, report_id: str) -> str:
    """Sign a (user_id, report_id) pair into an opaque, URL-safe token."""
    serializer = URLSafeTimedSerializer(secret_key, salt=_SALT)
    return serializer.dumps({"user_id": user_id, "report_id": report_id})


def verify_report_token(
    secret_key: str,
    token: str,
    report_id: str,
    *,
    max_age_seconds: int = REPORT_TOKEN_MAX_AGE_SECONDS,
) -> Optional[int]:
    """Return the signed user_id if `token` is valid, unexpired, and matches
    `report_id`; otherwise None. Never raises."""
    serializer = URLSafeTimedSerializer(secret_key, salt=_SALT)
    try:
        payload = serializer.loads(token, max_age=max_age_seconds)
    except (BadSignature, SignatureExpired):
        return None
    if not isinstance(payload, dict) or payload.get("report_id") != report_id:
        return None
    user_id = payload.get("user_id")
    return user_id if isinstance(user_id, int) else None


def report_token_expiry_date(*, max_age_seconds: int = REPORT_TOKEN_MAX_AGE_SECONDS) -> str:
    """Human-readable expiry date (YYYY-MM-DD) for a token signed right now."""
    expiry = _dt.datetime.now() + _dt.timedelta(seconds=max_age_seconds)
    return expiry.date().isoformat()
