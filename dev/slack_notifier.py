"""Slack webhook notifier for wp_links daily reports.

Adapted from /opt/projects/xflippa/dev/slack_notifier.py.
Posts mrkdwn-formatted messages to a Slack incoming webhook.
"""

import json
import logging

import requests

log = logging.getLogger(__name__)


def format_daily_summary(summary: dict, accounts_used: list[str]) -> str:
    """Format the daily edit summary as a Slack mrkdwn message."""
    parts = [
        f"*Wikipedia Link Fixer — Daily Report* — {summary['date']}",
        "",
        f"*Edits:* {summary['total_edits']} total | "
        f"{summary['successful_edits']} success | "
        f"{summary['failed_edits']} failed | "
        f"{summary['reverted_edits']} reverted",
    ]

    if accounts_used:
        accts = ", ".join(accounts_used)
        parts.append(f"*Accounts used:* {accts}")
    else:
        parts.append("*Accounts used:* none")

    return "\n".join(parts)


def format_error_message(account: str, error: str, edit_type: str) -> str:
    """Format an error alert for Slack."""
    return (
        f":warning: *Wikipedia Link Fixer — Error*\n"
        f"*Account:* {account}\n"
        f"*Edit type:* {edit_type}\n"
        f"*Error:* {error[:500]}"
    )


def format_diagnostic_message(account: str, analysis: str) -> str:
    """Format a diagnostic report from claude -p for Slack."""
    return (
        f":mag: *Wikipedia Link Fixer — Diagnostic Report*\n"
        f"*Account:* {account}\n"
        f"*Analysis:*\n{analysis[:1500]}"
    )


def send_notification(message: str, webhook_url: str | None) -> None:
    """POST a Slack message to the given incoming webhook URL.

    No-op if webhook_url is empty or None.
    Does not raise on failure — logs a warning instead.
    """
    if not webhook_url:
        log.debug("SLACK_WEBHOOK_URL not set — skipping Slack notification.")
        return

    payload = {"text": message}
    try:
        resp = requests.post(
            webhook_url,
            data=json.dumps(payload),
            headers={"Content-Type": "application/json"},
            timeout=10,
        )
        if resp.status_code != 200:
            log.warning("Slack webhook returned %d: %s", resp.status_code, resp.text[:200])
    except Exception as exc:
        log.warning("Failed to send Slack notification: %s", exc)
