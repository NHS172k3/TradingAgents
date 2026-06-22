"""Tests for automation.tokens — stateless signed report-link tokens."""

from __future__ import annotations

import time

from automation.tokens import (
    REPORT_TOKEN_MAX_AGE_SECONDS,
    report_token_expiry_date,
    sign_report_token,
    verify_report_token,
)

SECRET = "test-signing-key"


def test_sign_and_verify_round_trip():
    token = sign_report_token(SECRET, 123, "report-abc")

    assert verify_report_token(SECRET, token, "report-abc") == 123


def test_verify_rejects_token_for_a_different_report_id():
    token = sign_report_token(SECRET, 123, "report-abc")

    assert verify_report_token(SECRET, token, "report-xyz") is None


def test_verify_rejects_tampered_token():
    token = sign_report_token(SECRET, 123, "report-abc")
    tampered = token[:-1] + ("a" if token[-1] != "a" else "b")

    assert verify_report_token(SECRET, tampered, "report-abc") is None


def test_verify_rejects_token_signed_with_a_different_key():
    token = sign_report_token(SECRET, 123, "report-abc")

    assert verify_report_token("a-different-key", token, "report-abc") is None


def test_verify_rejects_expired_token():
    token = sign_report_token(SECRET, 123, "report-abc")
    time.sleep(1.1)

    assert verify_report_token(SECRET, token, "report-abc", max_age_seconds=1) is None


def test_report_token_expiry_date_is_max_age_seconds_in_the_future():
    import datetime as _dt

    expected = (_dt.datetime.now() + _dt.timedelta(seconds=REPORT_TOKEN_MAX_AGE_SECONDS)).date()

    assert report_token_expiry_date() == expected.isoformat()
