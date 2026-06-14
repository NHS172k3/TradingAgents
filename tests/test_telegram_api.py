"""Tests for Telegram API helpers."""

from __future__ import annotations

from automation.telegram_api import _split_message


def test_split_message_keeps_chunks_under_limit_at_newlines():
    text = "alpha\nbeta\ngamma"

    assert _split_message(text, 11) == ["alpha\nbeta", "gamma"]


def test_split_message_hard_splits_single_long_line():
    chunks = _split_message("ABCDEFGHIJ", 4)

    assert chunks == ["ABCD", "EFGH", "IJ"]
    assert all(len(chunk) <= 4 for chunk in chunks)
