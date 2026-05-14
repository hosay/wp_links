"""Daily edit orchestrator.

Selects accounts, rotates VPNs, executes edits (typo or link fix),
records results, and reports to Slack. Mirrors the pattern from
/opt/projects/xflippa/dev/run_with_notify.py.

Usage:
    python -m dev.orchestrator          # run daily edits
    python -m dev.orchestrator --dry    # dry run (no actual edits)
"""

import logging
import os
import random
import sys
import time
from datetime import datetime, timezone

from dotenv import load_dotenv

from dev.db import (
    init_db,
    get_account,
    get_accounts_by_state,
    update_account_state,
    increment_edit_count,
    add_page,
    get_pending_pages,
    claim_page,
    mark_page_done,
    get_fixable_links,
    add_edit,
    update_edit_status,
    get_daily_summary,
)
from dev.diagnostics import run_diagnostic
from dev.edit_engine import (
    load_typo_patterns,
    find_typo_in_text,
    apply_typo_fix,
    apply_link_fix,
    pick_typo_edit_summary,
)
from dev.fingerprint import load_fingerprint
from dev.link_finder import find_broken_links_in_article, fetch_dead_links_category
from dev.link_validator import (
    find_replacement,
    classify_confidence,
    generate_edit_summary,
)
from dev.slack_notifier import (
    format_daily_summary,
    format_error_message,
    format_diagnostic_message,
    send_notification,
)
from dev.vpn import vpn_session
from dev.wiki_browser import (
    create_browser,
    login,
    get_wikitext,
    save_edit,
    get_random_article_title,
    replace_url_in_wikitext,
)

load_dotenv()
log = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

DB_PATH = os.path.join(os.path.dirname(__file__), "wp_links.db")
PROFILES_DIR = os.path.join(os.path.dirname(__file__), "profiles")
WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")


# ── account selection ─────────────────────────────────────────────────


def select_daily_accounts(conn, count: int = 4) -> list:
    """Select accounts for today's edits.

    Prefers accounts that haven't been used recently.
    Excludes blocked/suspended/retired accounts.
    """
    eligible_states = ("warmup", "active")
    candidates = []
    for state in eligible_states:
        candidates.extend(get_accounts_by_state(conn, state))

    if not candidates:
        log.warning("No eligible accounts found!")
        return []

    # Sort by last_edit_at (None first = never used = highest priority)
    def sort_key(acct):
        last = acct["last_edit_at"]
        return last if last else ""

    candidates.sort(key=sort_key)

    # Take the top candidates (least recently used) with some randomness
    pool_size = min(len(candidates), count * 3)
    pool = candidates[:pool_size]
    selected = random.sample(pool, min(count, len(pool)))

    return selected


def determine_edit_type(state: str, edit_count: int) -> str:
    """Determine what type of edit an account should make."""
    if state == "warmup":
        return "typo"
    return "link_fix"


def should_transition_state(state: str, edit_count: int) -> bool:
    """Check if an account should transition from warmup to active."""
    return state == "warmup" and edit_count >= 2


# ── edit execution ────────────────────────────────────────────────────


def execute_typo_edit(page, conn, account) -> dict:
    """Execute a typo fix edit on a random article."""
    patterns = load_typo_patterns()
    max_attempts = 5

    for attempt in range(max_attempts):
        title = get_random_article_title(page)
        if not title:
            continue

        wikitext = get_wikitext(page, title)
        match = find_typo_in_text(wikitext, patterns)
        if not match:
            log.info("No typo found in %s, trying another article...", title)
            time.sleep(random.uniform(2, 4))
            continue

        fixed_text, count = apply_typo_fix(wikitext, match["wrong"], match["correct"])
        summary = pick_typo_edit_summary()

        # Record in DB
        page_id = add_page(conn, wiki_title=title, found_via="random")
        claim_page(conn, page_id, account["id"])
        edit_id = add_edit(
            conn,
            account_id=account["id"],
            page_id=page_id,
            edit_type="typo",
            diff_summary=f"{match['wrong']} -> {match['correct']} ({count}x)",
        )

        success = save_edit(page, title, fixed_text, summary)
        if success:
            update_edit_status(conn, edit_id, status="success")
            mark_page_done(conn, page_id)
            return {"success": True, "title": title, "edit_id": edit_id}
        else:
            update_edit_status(conn, edit_id, status="failed",
                             error_message="save_edit returned False")
            return {"success": False, "title": title, "edit_id": edit_id}

    return {"success": False, "title": None, "error": "No typo found after max attempts"}


def execute_link_fix(page, conn, account) -> dict:
    """Execute a link fix edit using a pre-discovered broken link."""
    # Get a fixable link from the DB
    fixable = get_fixable_links(conn)
    if not fixable:
        log.warning("No fixable links available — falling back to discovery")
        # Try to discover broken links on the fly
        results = fetch_dead_links_category(page, max_pages=1)
        if not results:
            return {"success": False, "error": "No broken links found"}

        # Check the first result for a broken link
        for result in results[:3]:
            title = result["wiki_title"]
            broken_urls = find_broken_links_in_article(page, title)
            if broken_urls:
                for url in broken_urls[:2]:
                    replacement = find_replacement(page, url)
                    if replacement:
                        from dev.db import add_broken_link, set_replacement_url
                        page_id = add_page(conn, wiki_title=title, found_via="wp_report")
                        bl_id = add_broken_link(conn, page_id, url, 404, "wp_report")
                        confidence = classify_confidence(
                            url, replacement["replacement_url"], replacement["source"]
                        )
                        if confidence == "high":
                            set_replacement_url(
                                conn, bl_id, replacement["replacement_url"],
                                confidence, replacement["source"]
                            )
                            fixable = get_fixable_links(conn)
                            break
            if fixable:
                break

    if not fixable:
        return {"success": False, "error": "No high-confidence replacements found"}

    # Pick one
    link = fixable[0]
    title = link["wiki_title"]

    # Fetch current wikitext
    wikitext = get_wikitext(page, title)
    if link["original_url"] not in wikitext:
        log.warning("Original URL no longer in %s — skipping", title)
        return {"success": False, "error": "URL no longer in article"}

    # Apply the fix
    fixed_text = apply_link_fix(wikitext, link["original_url"], link["replacement_url"])
    summary = generate_edit_summary(link["source"], link["original_url"], link["replacement_url"])

    # Record
    page_id = link["page_id"]
    claim_page(conn, page_id, account["id"])
    edit_id = add_edit(
        conn,
        account_id=account["id"],
        page_id=page_id,
        edit_type="link_fix",
        diff_summary=f"{link['original_url']} -> {link['replacement_url']}",
    )

    success = save_edit(page, title, fixed_text, summary)
    if success:
        update_edit_status(conn, edit_id, status="success")
        mark_page_done(conn, page_id)
        return {"success": True, "title": title, "edit_id": edit_id}
    else:
        update_edit_status(conn, edit_id, status="failed",
                         error_message="save_edit returned False")
        return {"success": False, "title": title, "edit_id": edit_id}


# ── main orchestration ────────────────────────────────────────────────


def run(dry_run: bool = False):
    """Execute the daily edit cycle."""
    log.info("=== Starting daily edit cycle ===")
    conn = init_db(DB_PATH)
    accounts = select_daily_accounts(conn, count=4)

    if not accounts:
        log.error("No eligible accounts — aborting")
        send_notification(
            ":warning: *Wikipedia Link Fixer* — No eligible accounts for today's run",
            WEBHOOK_URL,
        )
        return

    accounts_used = []
    for i, account in enumerate(accounts):
        username = account["username"]
        log.info("--- Account %d/%d: %s ---", i + 1, len(accounts), username)

        if dry_run:
            log.info("[DRY RUN] Would execute edit for %s", username)
            accounts_used.append(username)
            continue

        try:
            # Load fingerprint
            fingerprint = load_fingerprint(username, PROFILES_DIR)
            vpn_conf = account["vpn_conf_path"]

            with vpn_session(vpn_conf):
                with create_browser(fingerprint, account["profile_dir"]) as browser:
                    page = browser.new_page()

                    # Login
                    if not login(page, username, account["password"]):
                        # Check if account might be blocked
                        page_content = page.content().lower()
                        if any(m in page_content for m in ["bloqueado", "blocked"]):
                            log.error("Account %s is BLOCKED — marking as blocked", username)
                            update_account_state(conn, username, "blocked")
                            send_notification(
                                f":no_entry: *Account blocked*: {username}",
                                WEBHOOK_URL,
                            )
                            continue
                        raise RuntimeError(f"Login failed for {username}")

                    # Determine edit type
                    edit_type = determine_edit_type(account["state"], account["edit_count"])
                    log.info("Edit type for %s: %s", username, edit_type)

                    # Execute edit
                    if edit_type == "typo":
                        result = execute_typo_edit(page, conn, account)
                    else:
                        result = execute_link_fix(page, conn, account)

                    if result.get("success"):
                        log.info("Edit successful for %s on %s", username, result.get("title"))
                        increment_edit_count(conn, username)

                        # Check state transition
                        updated = get_account(conn, username)
                        if should_transition_state(updated["state"], updated["edit_count"]):
                            update_account_state(conn, username, "active")
                            log.info("Account %s transitioned to active", username)
                    else:
                        error = result.get("error", "Unknown error")
                        log.error("Edit failed for %s: %s", username, error)
                        send_notification(
                            format_error_message(username, error, edit_type),
                            WEBHOOK_URL,
                        )

                    accounts_used.append(username)

        except Exception as exc:
            error_str = str(exc)
            log.exception("Error processing account %s", username)
            send_notification(
                format_error_message(username, error_str, "unknown"),
                WEBHOOK_URL,
            )

            # Run diagnostics
            analysis = run_diagnostic(
                username, "unknown", error_str,
                "N/A",
            )
            send_notification(
                format_diagnostic_message(username, analysis),
                WEBHOOK_URL,
            )

        # Random delay between accounts (5-15 min)
        if i < len(accounts) - 1:
            delay = random.uniform(300, 900)
            log.info("Waiting %.0f seconds before next account...", delay)
            time.sleep(delay)

    # Daily summary
    summary = get_daily_summary(conn)
    msg = format_daily_summary(summary, accounts_used)
    log.info("Daily summary:\n%s", msg)
    send_notification(msg, WEBHOOK_URL)

    conn.close()
    log.info("=== Daily edit cycle complete ===")


if __name__ == "__main__":
    dry_run = "--dry" in sys.argv
    run(dry_run=dry_run)
