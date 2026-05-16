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


def _user_link(username: str) -> str:
    """Format a Wikipedia user profile as a Slack mrkdwn link."""
    return f"<https://es.wikipedia.org/wiki/Usuario:{username}|{username}>"


def _article_link(title: str) -> str:
    """Format an article title as a Slack mrkdwn link."""
    encoded = title.replace(" ", "_")
    return f"<https://es.wikipedia.org/wiki/{encoded}|{title}>"


def _diff_link(title: str, revision_id: str = None) -> str:
    """Format a diff link. Falls back to article history if no revision ID."""
    encoded = title.replace(" ", "_")
    if revision_id:
        url = f"https://es.wikipedia.org/w/index.php?title={encoded}&diff=prev&oldid={revision_id}"
    else:
        url = f"https://es.wikipedia.org/w/index.php?title={encoded}&action=history"
    return f"<{url}|{title}>"


def _cost_line(usage: dict) -> str:
    """Single-line cost summary."""
    tavily_cost = usage.get("tavily_searches", 0) * 0.008
    gemini_tokens = usage.get("gemini_input_tokens", 0) + usage.get("gemini_output_tokens", 0)
    gemini_cost = (
        usage.get("gemini_input_tokens", 0) * 0.000000075
        + usage.get("gemini_output_tokens", 0) * 0.0000003
    )
    total = tavily_cost + gemini_cost
    return (
        f"Tavily: {usage.get('tavily_searches', 0)} searches (${tavily_cost:.3f}) | "
        f"Gemini: {usage.get('gemini_calls', 0)} calls, {gemini_tokens} tokens (${gemini_cost:.4f}) | "
        f"*Total: ${total:.3f}*"
    )


def _pipeline_section(account_summary: dict, fixable_count: int = 0) -> list[str]:
    """Account pipeline status lines."""
    warmup = account_summary.get("warmup_count", 0)
    active = account_summary.get("active_count", 0)
    pending = account_summary.get("pending_count", 0)
    blocked = account_summary.get("blocked_count", 0)
    avg = account_summary.get("avg_warmup_progress", 0.0)
    total = account_summary.get("total", 0)

    # Progress bar
    filled = int(avg)
    bar = "█" * filled + "░" * (5 - filled)

    lines = [
        "*Account Pipeline* (warmup = typo/spacing edits → 5 edits = active for link fixes):",
        f"• Warmup: {warmup}/{total} — {avg}/5 edits completed ({bar})",
        f"• Active: {active}/{total} — doing link fixes",
    ]
    if pending:
        lines.append(f"• Pending: {pending} — never used yet")
    if blocked:
        lines.append(f"• Blocked/Suspended: {blocked} :warning:")
    if fixable_count:
        lines.append(f"• Link fixes waiting: {fixable_count} replacement URLs found — will be applied once accounts reach active state")
    # Estimate time to activation
    if warmup > 0 and avg < 5:
        edits_needed = int((5 - avg) * warmup)
        lines.append(f"• _~{edits_needed} warmup edits remaining before first accounts activate_")
    return lines


def format_discovery_report(
    stats: dict, usage: dict, fixable_links: list, account_summary: dict = None
) -> str:
    """Format the discovery pipeline report for Slack."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    replacements = stats.get('replacements_found', 0)
    broken = stats.get('broken_urls_found', 0)
    efficiency = f"{(replacements / broken * 100):.1f}%" if broken else "—"
    tavily_cost = usage.get("tavily_searches", 0) * 0.008
    cost_per_fix = f"${tavily_cost / replacements:.3f}" if replacements else "—"

    parts = [
        f":mag: *Wikipedia Link Fixer — Discovery Report* — {now}",
        "",
        "*Pipeline:*",
        f"• Articles scanned: {stats.get('articles_checked', 0)}",
        f"• Broken URLs found: {broken}",
        f"• Replacements found: {replacements} "
        f"({stats.get('high_confidence', 0)} high, {stats.get('medium_confidence', 0)} medium)",
        f"• Replacement rate: {efficiency} | Cost/fix: {cost_per_fix}",
    ]

    # Replacement details with URLs
    if fixable_links:
        parts.append("")
        parts.append("*Replacements Ready:*")
        for link in fixable_links[:8]:
            title = link["wiki_title"]
            confidence = "✓" if link["confidence"] == "high" else "~"
            source = link["source"]
            original = link["original_url"]
            replacement = link["replacement_url"]
            # Truncate URLs to domain + first path segment
            from urllib.parse import urlparse
            orig_parsed = urlparse(original)
            repl_parsed = urlparse(replacement)
            orig_short = orig_parsed.netloc + orig_parsed.path[:30]
            repl_short = repl_parsed.netloc + repl_parsed.path[:30]
            parts.append(
                f"• {confidence} {_article_link(title)} [{source}]"
            )
            parts.append(f"   `{orig_short}` → `{repl_short}`")

    # Account pipeline (compact for discovery report)
    if account_summary:
        parts.append("")
        warmup = account_summary.get("warmup_count", 0)
        active = account_summary.get("active_count", 0)
        avg = account_summary.get("avg_warmup_progress", 0.0)
        parts.append(
            f"*Accounts:* Warmup {warmup} (avg {avg}/5) | Active {active} | "
            f"Fixes queued: {len(fixable_links)}"
        )

    # Cost
    parts.append("")
    parts.append(f"*Cost:* {_cost_line(usage)}")

    return "\n".join(parts)


def format_edit_report(
    edits: list,
    accounts_used: list[str],
    usage: dict,
    account_summary: dict = None,
    fixable_count: int = 0,
    health: dict = None,
) -> str:
    """Format the daily edit cycle report for Slack.

    Args:
        edits: List of dicts: {account, time, edit_type, title, status, revision_id, error_message}
        accounts_used: Usernames that participated.
        usage: API usage from link_replacer.get_usage_stats().
        account_summary: From get_account_pipeline_summary().
        fixable_count: Number of link fixes queued.
        health: Optional dict with {reverted_7d, total_7d, admin_warnings}.
    """
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    total = len(edits)
    success = sum(1 for e in edits if e.get("status") == "success")
    failed = sum(1 for e in edits if e.get("status") == "failed")
    total_accounts = account_summary.get("total", 20) if account_summary else 20

    parts = [
        f":pencil2: *Wikipedia Link Fixer — Edit Report* — {now}",
        "",
        f"*Summary:* {total} edits | {success} success | {failed} failed",
        f"*Accounts used:* {len(accounts_used)}/{total_accounts}",
    ]

    # Health dashboard
    if health:
        reverted = health.get("reverted_7d", 0)
        total_7d = health.get("total_7d", 0)
        warnings = health.get("admin_warnings", 0)
        accept_rate = f"{((total_7d - reverted) / total_7d * 100):.0f}%" if total_7d else "—"
        parts.append("")
        parts.append("*Health (7d):*")
        status = "✓" if reverted == 0 and warnings == 0 else "⚠"
        parts.append(f"• {status} Edits accepted: {total_7d - reverted}/{total_7d} ({accept_rate})")
        parts.append(f"• {'✓' if reverted == 0 else '⚠'} Reversions: {reverted}")
        if warnings:
            parts.append(f"• :warning: Admin warnings: {warnings}")

    # Edits table with linked accounts and articles
    if edits:
        parts.append("")
        parts.append("*Edits:*")
        for e in edits:
            status_icon = "✓" if e.get("status") == "success" else "✗"
            acct = _user_link(e.get("account", "?"))
            title = e.get("title", "N/A")
            etype = e.get("edit_type", "?")
            time_str = e.get("time", "?")
            article = _article_link(title) if title != "N/A" else "N/A"
            parts.append(f"• {status_icon} {acct} — _{etype}_ on {article} ({time_str})")

    # Diff links for successful edits
    successful = [e for e in edits if e.get("status") == "success" and e.get("title")]
    if successful:
        parts.append("")
        parts.append("*Diffs:*")
        for e in successful:
            revision_id = e.get("revision_id")
            diff = _diff_link(e["title"], revision_id)
            acct = e.get("account", "?")
            parts.append(f"• {diff} — {acct}")

    # Failures with error context
    failures = [e for e in edits if e.get("status") == "failed"]
    if failures:
        parts.append("")
        parts.append("*Failures:*")
        for e in failures:
            acct = _user_link(e.get("account", "?"))
            error = e.get("error_message", "unknown error")[:200]
            etype = e.get("edit_type", "?")
            parts.append(f"• {acct} ({etype}): _{error}_")

    # Account pipeline
    if account_summary:
        parts.append("")
        parts.extend(_pipeline_section(account_summary, fixable_count))

    # Cost
    parts.append("")
    parts.append(f"*Cost:* {_cost_line(usage)}")

    return "\n".join(parts)


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
