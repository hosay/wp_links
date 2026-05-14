"""Tests for dev.slack_notifier — Slack webhook notifications."""

from unittest.mock import patch, MagicMock

import pytest

from dev.slack_notifier import (
    format_daily_summary,
    format_error_message,
    send_notification,
)


def test_format_daily_summary():
    summary = {
        "date": "2026-05-14",
        "total_edits": 4,
        "successful_edits": 3,
        "failed_edits": 1,
        "reverted_edits": 0,
    }
    accounts_used = ["editor1", "editor2", "editor3", "editor4"]
    msg = format_daily_summary(summary, accounts_used)
    assert "2026-05-14" in msg
    assert "4" in msg
    assert "editor1" in msg


def test_format_daily_summary_no_edits():
    summary = {
        "date": "2026-05-14",
        "total_edits": 0,
        "successful_edits": 0,
        "failed_edits": 0,
        "reverted_edits": 0,
    }
    msg = format_daily_summary(summary, [])
    assert "0" in msg


def test_format_error_message():
    msg = format_error_message(
        account="editor1",
        error="TimeoutError: Page.click timed out",
        edit_type="link_fix",
    )
    assert "editor1" in msg
    assert "TimeoutError" in msg


@patch("dev.slack_notifier.requests.post")
def test_send_notification(mock_post):
    mock_post.return_value = MagicMock(status_code=200)
    send_notification("test message", "https://hooks.slack.com/test")
    mock_post.assert_called_once()


@patch("dev.slack_notifier.requests.post")
def test_send_notification_no_webhook(mock_post):
    send_notification("test message", "")
    mock_post.assert_not_called()


@patch("dev.slack_notifier.requests.post")
def test_send_notification_handles_failure(mock_post):
    mock_post.side_effect = Exception("Connection error")
    # Should not raise
    send_notification("test message", "https://hooks.slack.com/test")
