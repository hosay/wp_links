"""Daily edit orchestrator.

Selects accounts, executes edits (typo or link fix) via residential proxy,
records results, and reports to Slack. Mirrors the pattern from
/opt/projects/xflippa/dev/run_with_notify.py.

Usage:
    python -m dev.orchestrator          # run daily edits
    python -m dev.orchestrator --dry    # dry run (no actual edits)
"""

import json
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
    get_account_pipeline_summary,
    get_health_metrics,
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
    mark_page_unclaimed,
)
from dev.diagnostics import run_diagnostic
from dev.edit_engine import (
    load_typo_patterns,
    find_typo_in_text,
    apply_typo_fix,
    apply_link_fix,
    pick_typo_edit_summary,
    search_articles_with_typo,
    find_double_spaces,
    apply_spacing_fix,
    pick_spacing_edit_summary,
)
from dev.account_creator import build_proxy
from dev.fingerprint import load_fingerprint
from dev.discovery import discover_broken_links
from dev.link_finder import find_broken_links_in_article, fetch_dead_links_category
from dev.link_validator import (
    find_replacement,
    classify_confidence,
    generate_edit_summary,
)
from dev.link_replacer import get_usage_stats, reset_usage_stats
from dev.slack_notifier import (
    format_daily_summary,
    format_edit_report,
    format_error_message,
    format_diagnostic_message,
    send_notification,
)
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
    """Determine what type of edit an account should make.

    During warmup, alternates between typo and spacing fixes to look natural.
    Typo fix on odd edit counts, spacing fix on even — ensures variety across
    the 5-edit warmup period.
    """
    if state == "warmup":
        return "typo" if edit_count % 2 == 0 else "spacing"
    return "link_fix"


def should_transition_state(state: str, edit_count: int) -> bool:
    """Check if an account should transition from warmup to active."""
    return state == "warmup" and edit_count >= 5


# ── edit execution ────────────────────────────────────────────────────


def execute_typo_edit(page, conn, account) -> dict:
    """Execute a typo fix edit using MediaWiki API to find a suitable article."""
    patterns = load_typo_patterns()
    random.shuffle(patterns)  # vary which typo we target each run

    for pattern in patterns:
        candidates = search_articles_with_typo(pattern["wrong"], limit=20)
        if not candidates:
            continue

        random.shuffle(candidates)
        for title in candidates[:10]:
            wikitext = get_wikitext(page, title)
            match = find_typo_in_text(wikitext, patterns)
            if not match:
                log.info("No typo confirmed in %s (false positive from API), skipping", title)
                time.sleep(random.uniform(1, 3))
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
                mark_page_unclaimed(conn, page_id)
                return {"success": False, "title": title, "edit_id": edit_id}

    return {"success": False, "title": None, "error": "No typo found after exhausting all patterns"}


def execute_spacing_edit(page, conn, account) -> dict:
    """Fix double spaces in a random article (warmup variety edit)."""
    max_attempts = 15
    for _ in range(max_attempts):
        title = get_random_article_title(page)
        if not title:
            continue
        wikitext = get_wikitext(page, title)
        if not find_double_spaces(wikitext):
            time.sleep(random.uniform(1, 3))
            continue

        fixed_text, count = apply_spacing_fix(wikitext)
        summary = pick_spacing_edit_summary()

        page_id = add_page(conn, wiki_title=title, found_via="random")
        claim_page(conn, page_id, account["id"])
        edit_id = add_edit(
            conn,
            account_id=account["id"],
            page_id=page_id,
            edit_type="spacing",
            diff_summary=f"double spaces removed ({count}x)",
        )

        success = save_edit(page, title, fixed_text, summary)
        if success:
            update_edit_status(conn, edit_id, status="success")
            mark_page_done(conn, page_id)
            return {"success": True, "title": title, "edit_id": edit_id}
        else:
            update_edit_status(conn, edit_id, status="failed",
                               error_message="save_edit returned False")
            mark_page_unclaimed(conn, page_id)
            return {"success": False, "title": title, "edit_id": edit_id}

    return {"success": False, "title": None, "error": "No double-space article found after max attempts"}


def execute_link_fix(page, conn, account) -> dict:
    """Execute a link fix edit using a pre-discovered broken link."""
    # Get a fixable link from the DB
    fixable = get_fixable_links(conn)
    if not fixable:
        log.warning("No fixable links available — running discovery")
        # Run discovery pipeline (uses API + Google Search, no Camoufox needed)
        stats = discover_broken_links(conn, max_articles=20)
        log.info("Discovery found %d replacements", stats.get("replacements_found", 0))
        fixable = get_fixable_links(conn)

    if not fixable:
        return {"success": False, "error": "No fixable replacements found after discovery"}

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
        mark_page_unclaimed(conn, page_id)
        return {"success": False, "title": title, "edit_id": edit_id}


# ── proxy fallback ───────────────────────────────────────────────────


def _build_proxy_fallbacks(proxy_config: dict, username: str) -> list[tuple[str, dict | None]]:
    """Build a list of proxy configs with decreasing geo specificity.

    If city-level proxy is unavailable, try region-only, then country-only.
    Returns list of (label, proxy_dict) tuples.
    """
    if not proxy_config:
        return [("no proxy", None)]

    fallbacks = []

    # Full specificity: country + region + city
    if proxy_config.get("city"):
        fallbacks.append((
            f"{proxy_config['country']}/{proxy_config.get('region', '')}/{proxy_config['city']}",
            build_proxy(proxy_config, session_id=username),
        ))

    # Region only (drop city)
    if proxy_config.get("region"):
        region_config = {k: v for k, v in proxy_config.items() if k != "city"}
        fallbacks.append((
            f"{proxy_config['country']}/{proxy_config['region']}",
            build_proxy(region_config, session_id=username),
        ))

    # Country only (drop region and city)
    country_config = {"country": proxy_config["country"]}
    fallbacks.append((
        f"{proxy_config['country']} only",
        build_proxy(country_config, session_id=username),
    ))

    return fallbacks


# ── main orchestration ────────────────────────────────────────────────


def run(dry_run: bool = False):
    """Execute the daily edit cycle."""
    log.info("=== Starting daily edit cycle ===")
    conn = init_db(DB_PATH)
    daily_count = random.randint(2, 5)
    accounts = select_daily_accounts(conn, count=daily_count)

    if not accounts:
        log.error("No eligible accounts — aborting")
        send_notification(
            ":warning: *Wikipedia Link Fixer* — No eligible accounts for today's run",
            WEBHOOK_URL,
        )
        return

    accounts_used = []
    edit_records = []  # Track all edits for reporting
    reset_usage_stats()

    for i, account in enumerate(accounts):
        username = account["username"]
        log.info("--- Account %d/%d: %s ---", i + 1, len(accounts), username)

        if dry_run:
            log.info("[DRY RUN] Would execute edit for %s", username)
            accounts_used.append(username)
            continue

        try:
            # Load fingerprint and proxy config
            fingerprint = load_fingerprint(username, PROFILES_DIR)
            proxy_config = json.loads(account["connection_config"]) if account["connection_config"] else {}

            # Try proxy with full geo specificity, fall back to less specific on failure
            proxy_attempts = _build_proxy_fallbacks(proxy_config, username)
            working_proxy = None
            for attempt_label, proxy in proxy_attempts:
                try:
                    # Quick validation: test proxy connectivity before launching browser
                    from dev.account_creator import PROXY_HOST, PROXY_PORT, PROXY_USER, PROXY_PASS
                    import requests as _req
                    test_proxy_url = f"http://{proxy['username']}:{proxy['password']}@{PROXY_HOST}:{PROXY_PORT}" if proxy else None
                    if test_proxy_url:
                        _req.get("http://httpbin.org/ip",
                                 proxies={"http": test_proxy_url, "https": test_proxy_url},
                                 timeout=8)
                    working_proxy = proxy
                    log.info("Proxy validated: %s", attempt_label)
                    break
                except Exception as proxy_exc:
                    log.warning("Proxy failed (%s): %s — trying next fallback",
                                attempt_label, str(proxy_exc)[:100])
                    continue

            if working_proxy is None and proxy_config:
                raise RuntimeError(f"All proxy fallbacks failed for {username}")

            # Create account on Wikipedia if not yet registered
            # Must happen BEFORE opening the edit browser (can't nest Playwright)
            is_registered = account["registered"] if "registered" in account.keys() else 0
            if not is_registered:
                log.info("Account %s not yet registered — creating on Wikipedia...", username)
                from dev.account_creator import create_account
                created = create_account(username, account["password"], proxy_config)
                if created:
                    log.info("Account %s registered successfully — skipping edit this run (first login next run)", username)
                    conn.execute(
                        "UPDATE accounts SET registered = 1 WHERE username = ?",
                        (username,),
                    )
                    conn.commit()
                    edit_records.append({
                        "account": username, "time": datetime.now(timezone.utc).strftime("%H:%M"),
                        "edit_type": "registration", "title": "N/A",
                        "status": "success", "revision_id": None,
                        "error_message": "",
                    })
                    accounts_used.append(username)
                    continue  # First login on next run — avoids CAPTCHA accumulation
                else:
                    log.warning("Account creation failed for %s — skipping to next account", username)
                    edit_records.append({
                        "account": username, "time": datetime.now(timezone.utc).strftime("%H:%M"),
                        "edit_type": "registration", "title": "N/A",
                        "status": "failed", "revision_id": None,
                        "error_message": "Wikipedia account creation failed",
                    })
                    continue

            with create_browser(fingerprint, account["profile_dir"], proxy=working_proxy) as browser:
                page = browser.new_page()

                # Login
                if not login(page, username, account["password"]):
                    raise RuntimeError(f"Login failed for {username}")

                # Determine edit type
                edit_type = determine_edit_type(account["state"], account["edit_count"])
                log.info("Edit type for %s: %s", username, edit_type)

                # Execute edit
                if edit_type == "typo":
                    result = execute_typo_edit(page, conn, account)
                elif edit_type == "spacing":
                    result = execute_spacing_edit(page, conn, account)
                else:
                    result = execute_link_fix(page, conn, account)

                # Record for reporting
                edit_time = datetime.now(timezone.utc).strftime("%H:%M")
                title = result.get("title", "N/A")
                status = "success" if result.get("success") else "failed"
                revision_id = result.get("revision_id")
                error_msg = result.get("error", "") if not result.get("success") else ""

                edit_records.append({
                    "account": username,
                    "time": edit_time,
                    "edit_type": edit_type,
                    "title": title or "N/A",
                    "status": status,
                    "revision_id": revision_id,
                    "error_message": error_msg,
                })

                if result.get("success"):
                    log.info("Edit successful for %s on %s", username, title)
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

        # Random delay between accounts (1-90 min) for organic timing
        if i < len(accounts) - 1:
            delay = random.uniform(60, 5400)
            log.info("Waiting %.0f seconds (%.1f min) before next account...", delay, delay / 60)
            time.sleep(delay)

    # Send detailed edit report
    usage = get_usage_stats()
    account_summary = get_account_pipeline_summary(conn)
    health = get_health_metrics(conn)
    fixable = get_fixable_links(conn)
    report = format_edit_report(
        edit_records, accounts_used, usage,
        account_summary=account_summary,
        fixable_count=len(fixable),
        health=health,
    )
    log.info("Edit report:\n%s", report)
    send_notification(report, WEBHOOK_URL)

    conn.close()
    log.info("=== Daily edit cycle complete ===")


if __name__ == "__main__":
    dry_run = "--dry" in sys.argv
    run(dry_run=dry_run)
