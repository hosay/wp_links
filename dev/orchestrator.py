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
    get_all_account_summaries,
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
    mark_link_stale,
    reassign_proxy,
)
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
    send_notification,
)
from dev.wiki_browser import (
    create_browser,
    login,
    get_wikitext,
    save_edit,
    verify_edit_landed,
    get_random_article_title,
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


def select_daily_accounts(conn, count: int = 5) -> list:
    """Select registered accounts for today's edits.

    Only picks registered accounts (unregistered ones are handled in
    the separate registration phase). Prefers accounts that haven't
    been used recently. Excludes blocked/suspended/retired accounts
    and those still in cooldown.
    """
    eligible_states = ("warmup", "active")
    candidates = []
    for state in eligible_states:
        candidates.extend(get_accounts_by_state(conn, state))

    # Filter: registered only, not in block cooldown
    now_iso = datetime.now(timezone.utc).isoformat()
    candidates = [
        a for a in candidates
        if a["registered"]
        and (not a["blocked_until"] or a["blocked_until"] <= now_iso)
    ]

    if not candidates:
        log.warning("No registered eligible accounts found!")
        return []

    # Sort by last_edit_at (None first = never used = highest priority)
    def sort_key(acct):
        last = acct["last_edit_at"]
        return last if last else ""

    candidates.sort(key=sort_key)

    # Sample from top candidates for variety
    pool_size = min(len(candidates), count * 3)
    sample_pool = candidates[:pool_size]
    selected = random.sample(sample_pool, min(count, len(sample_pool)))

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


def qa_check_edit(title: str, original_text: str, edited_text: str, edit_type: str) -> tuple[bool, str]:
    """Use Gemini to QA-check an edit before saving.

    Sends a concise diff context to Gemini and asks if the edit is correct.
    Returns (approved, reason).
    """
    import requests as _req
    import difflib

    gemini_key = os.environ.get("GOOGLE_GEMENI_CONTENT_CREATOR", "")
    if not gemini_key:
        log.warning("No Gemini key — skipping QA check")
        return True, "no_gemini_key"

    # Build a concise diff (only changed lines with context)
    orig_lines = original_text.splitlines()
    edit_lines = edited_text.splitlines()
    context_lines = 5 if edit_type == "link_fix" else 2
    diff = list(difflib.unified_diff(orig_lines, edit_lines, n=context_lines, lineterm=""))
    if not diff:
        return False, "no_changes_detected"

    diff_text = "\n".join(diff[:100])  # Cap at 100 lines

    link_fix_guidance = ""
    if edit_type == "link_fix":
        link_fix_guidance = (
            "- For link_fix edits, verify:\n"
            "  * The old broken URL is replaced with a new live URL\n"
            "  * Dead-link markers are REMOVED: {{enlace roto}}, {{URL inaccesible}}, "
            "|urlmuerta=sí, |estado=muerto, |dead-url=yes, |url-status=dead\n"
            "  * The reference tag is NOT left empty — it must still contain a valid URL\n"
            "  * Only the targeted broken link's markers are removed, not other links' markers\n"
        )

    prompt = (
        f"You are a Spanish Wikipedia editor reviewing a proposed '{edit_type}' edit to the article \"{title}\".\n\n"
        f"Here is the diff (unified format):\n```\n{diff_text}\n```\n\n"
        "Is this edit CORRECT? Consider:\n"
        "- Does it fix a genuine error, or does it break intentional content?\n"
        f"{link_fix_guidance}"
        "- In linguistic/technical articles, unusual forms may be intentional examples.\n"
        "- Asterisk (*) before a word in linguistics means 'ungrammatical/hypothetical form' — do NOT 'fix' these.\n"
        "- Words in quotes or italics may be intentional examples, not errors.\n\n"
        "Reply with ONLY one line: 'APPROVE' or 'REJECT: <brief reason>'"
    )

    try:
        resp = _req.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={gemini_key}",
            json={"contents": [{"parts": [{"text": prompt}]}]},
            timeout=20,
            headers={"Content-Type": "application/json"},
        )
        resp.raise_for_status()
        answer = resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
        log.info("QA check for %s: %s", title, answer[:100])

        if answer.upper().startswith("APPROVE"):
            return True, "approved"
        else:
            reason = answer.replace("REJECT:", "").replace("REJECT", "").strip()
            return False, reason or "rejected_by_qa"

    except Exception as exc:
        log.warning("QA check failed for %s: %s — approving by default", title, exc)
        return True, f"qa_error:{exc}"


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

            # QA check with Gemini before saving
            approved, qa_reason = qa_check_edit(title, wikitext, fixed_text, "typo")
            if not approved:
                log.warning("QA REJECTED edit on %s: %s — skipping", title, qa_reason)
                time.sleep(random.uniform(1, 3))
                continue

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
                return {"success": True, "title": title, "edit_id": edit_id, "page_id": page_id}
            else:
                update_edit_status(conn, edit_id, status="failed",
                                 error_message="save_edit returned False")
                mark_page_unclaimed(conn, page_id)
                return {"success": False, "title": title, "edit_id": edit_id, "page_id": page_id}

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

        # QA check
        approved, qa_reason = qa_check_edit(title, wikitext, fixed_text, "spacing")
        if not approved:
            log.warning("QA REJECTED spacing edit on %s: %s — skipping", title, qa_reason)
            time.sleep(random.uniform(1, 3))
            continue

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
            return {"success": True, "title": title, "edit_id": edit_id, "page_id": page_id}
        else:
            update_edit_status(conn, edit_id, status="failed",
                               error_message="save_edit returned False")
            mark_page_unclaimed(conn, page_id)
            return {"success": False, "title": title, "edit_id": edit_id, "page_id": page_id}

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

    # Try fixable links, skipping any where the URL has been removed
    link = None
    for candidate in fixable:
        title = candidate["wiki_title"]
        wikitext = get_wikitext(page, title)
        if candidate["original_url"] in wikitext:
            link = candidate
            break
        log.warning("Original URL no longer in %s — marking stale", title)
        mark_link_stale(conn, candidate["id"], "url_removed_from_article")

    if not link:
        return {"success": False, "error": "All fixable links stale (URLs removed from articles)"}

    # Apply the fix
    fixed_text = apply_link_fix(wikitext, link["original_url"], link["replacement_url"])

    # QA check
    approved, qa_reason = qa_check_edit(title, wikitext, fixed_text, "link_fix")
    if not approved:
        log.warning("QA REJECTED link fix on %s: %s — marking replacement stale", title, qa_reason)
        mark_link_stale(conn, link["id"], f"qa_rejected:{qa_reason[:120]}")
        return {"success": False, "error": f"QA rejected: {qa_reason}"}

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
        return {"success": True, "title": title, "edit_id": edit_id, "page_id": page_id}
    else:
        update_edit_status(conn, edit_id, status="failed",
                         error_message="save_edit returned False")
        mark_page_unclaimed(conn, page_id)
        return {"success": False, "title": title, "edit_id": edit_id, "page_id": page_id}


# ── proxy rotation ───────────────────────────────────────────────────


def _test_proxy_connectivity(proxy_dict: dict, timeout: int = 15) -> bool:
    """Test proxy connectivity with a lightweight HTTP request to Wikipedia.

    Uses requests (not Camoufox) to avoid Playwright event loop issues
    when testing multiple proxies in sequence.
    """
    import requests as http_requests

    proxy_url = (
        f"http://{proxy_dict['username']}:{proxy_dict['password']}"
        f"@{proxy_dict['server'].replace('http://', '')}"
    )
    try:
        resp = http_requests.get(
            "https://es.wikipedia.org/w/api.php?action=query&meta=siteinfo&format=json",
            proxies={"https": proxy_url, "http": proxy_url},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=timeout,
        )
        return resp.status_code == 200
    except Exception:
        return False


def _find_working_proxy(account, username: str) -> tuple[dict | None, dict | None]:
    """Test proxies from the rotation pool and return the first that connects.

    Returns (proxy_config, proxy_dict) or (None, None) if all fail.
    If the account has no proxy assigned (empty config), returns ({}, None)
    to signal direct connection without proxy.

    Camoufox/Playwright can only be instantiated once per process, so we
    test connectivity with plain HTTP requests first.
    """
    from dev.data.proxy_pool import get_rotation_pool

    proxy_config = json.loads(account["connection_config"]) if account["connection_config"] else {}

    # If account has no proxy, use direct connection (no proxy)
    if not proxy_config:
        log.info("Account %s has no proxy assigned — using direct connection", username)
        return {}, None

    pool = get_rotation_pool(proxy_config)

    for i, candidate in enumerate(pool):
        label = f"{candidate.get('country', '?')}/{candidate.get('region', '')}/{candidate.get('city', '')}"
        log.info("Proxy test %d/%d for %s: %s", i + 1, len(pool), username, label)

        proxy_dict = build_proxy(candidate, session_id=username)

        if _test_proxy_connectivity(proxy_dict):
            log.info("Proxy %s is reachable for %s", label, username)
            return candidate, proxy_dict
        else:
            log.warning("Proxy %s unreachable for %s — trying next", label, username)
            if i < len(pool) - 1:
                time.sleep(3)

    log.error("All %d proxies exhausted for %s", len(pool), username)
    return None, None


# ── main orchestration ────────────────────────────────────────────────


def _register_pending_accounts(conn, max_register: int = 6) -> list[dict]:
    """Register unregistered accounts before the edit phase.

    Attempts to create up to max_register accounts on Wikipedia via API.
    Stops on rate limit. Returns list of edit_record dicts for reporting.
    """
    from dev.account_creator import create_account

    candidates = get_accounts_by_state(conn, "warmup") + get_accounts_by_state(conn, "active")
    now_iso = datetime.now(timezone.utc).isoformat()
    unregistered = [
        a for a in candidates
        if not a["registered"]
        and (not a["blocked_until"] or a["blocked_until"] <= now_iso)
    ]

    if not unregistered:
        return []

    # Registration cooldown: skip if recent attempts all failed (IP blocked everywhere)
    recent_failures = conn.execute(
        "SELECT COUNT(*) FROM accounts WHERE state IN ('username_taken', 'password_mismatch', "
        "'antispoof_blocked', 'password_lost') AND created_at > datetime('now', '-24 hours')"
    ).fetchone()[0]
    if recent_failures > 0:
        log.info("Registration cooldown active (%d recent failures) — skipping", recent_failures)
        return []

    random.shuffle(unregistered)
    to_register = unregistered[:max_register]
    records = []

    for account in to_register:
        username = account["username"]
        proxy_config = json.loads(account["connection_config"]) if account["connection_config"] else {}

        # Skip usernames that already exist in Wikimedia central auth
        try:
            import requests as _req
            _r = _req.get(
                "https://meta.wikimedia.org/w/api.php",
                params={"action": "query", "meta": "globaluserinfo", "guiuser": username, "format": "json"},
                headers={"User-Agent": "Mozilla/5.0"}, timeout=10,
            )
            if "missing" not in _r.json().get("query", {}).get("globaluserinfo", {}):
                log.warning("Username %s already exists in Wikimedia central auth — skipping registration", username)
                conn.execute("UPDATE accounts SET state = 'username_taken' WHERE username = ?", (username,))
                conn.commit()
                records.append({
                    "account": username, "time": datetime.now(timezone.utc).strftime("%H:%M"),
                    "edit_type": "registration", "title": "N/A",
                    "status": "failed", "revision_id": None,
                    "error_message": "Username exists in central auth",
                })
                continue
        except Exception:
            pass  # If check fails, proceed with registration attempt

        log.info("--- Registering %s on Wikipedia ---", username)
        created = create_account(username, account["password"], proxy_config, conn=conn)
        if created:
            log.info("Account %s registered successfully", username)
            conn.execute(
                "UPDATE accounts SET registered = 1 WHERE username = ?",
                (username,),
            )
            conn.commit()
            records.append({
                "account": username, "time": datetime.now(timezone.utc).strftime("%H:%M"),
                "edit_type": "registration", "title": "N/A",
                "status": "success", "revision_id": None,
                "error_message": "",
            })
        else:
            log.warning("Account creation failed for %s", username)
            records.append({
                "account": username, "time": datetime.now(timezone.utc).strftime("%H:%M"),
                "edit_type": "registration", "title": "N/A",
                "status": "failed", "revision_id": None,
                "error_message": "Wikipedia account creation failed",
            })

        # Delay between registrations to avoid rate limiting
        time.sleep(random.uniform(30, 90))

    return records


def run(dry_run: bool = False):
    """Execute the daily edit cycle."""
    log.info("=== Starting daily edit cycle ===")
    conn = init_db(DB_PATH)

    # Phase 1: Register pending accounts (separate from editing)
    registration_records = []
    if not dry_run:
        registration_records = _register_pending_accounts(conn, max_register=1)

    # Phase 2: Select registered accounts for editing
    daily_count = 7
    accounts = select_daily_accounts(conn, count=daily_count)

    if not accounts:
        log.error("No eligible accounts — aborting")
        return

    accounts_used = []
    edit_records = list(registration_records)  # Include registration records in report
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

            # Find a working proxy from the rotation pool
            working_proxy_config, working_proxy = _find_working_proxy(account, username)

            if working_proxy_config is None:
                # All proxies exhausted (distinct from empty config = no proxy)
                error_msg = f"All proxies exhausted for {username}"
                log.error(error_msg)
                edit_records.append({
                    "account": username,
                    "time": datetime.now(timezone.utc).strftime("%H:%M"),
                    "edit_type": determine_edit_type(account["state"], account["edit_count"]),
                    "title": "N/A",
                    "status": "failed",
                    "revision_id": None,
                    "error_message": error_msg,
                })
                continue

            # Persist working proxy if it differs from the account's stored one
            if working_proxy_config and working_proxy_config != proxy_config:
                reassign_proxy(conn, username, working_proxy_config)
                log.info("Saved new working proxy for %s: %s", username, working_proxy_config)

            with create_browser(fingerprint, account["profile_dir"], proxy=working_proxy) as browser:
                page = browser.new_page()

                # Login
                if not login(page, username, account["password"]):
                    log.error("Login failed for %s — marking as password_mismatch", username)
                    update_account_state(conn, username, "password_mismatch")
                    edit_records.append({
                        "account": username,
                        "time": datetime.now(timezone.utc).strftime("%H:%M"),
                        "edit_type": determine_edit_type(account["state"], account["edit_count"]),
                        "title": "N/A",
                        "status": "failed",
                        "revision_id": None,
                        "error_message": "Login failed — marked password_mismatch",
                    })
                    continue

                # Determine edit type
                edit_type = determine_edit_type(account["state"], account["edit_count"])
                log.info("Edit type for %s: %s", username, edit_type)

                # Execute edit — active accounts fall back to typo/spacing
                # when no fixable links are available
                if edit_type == "typo":
                    result = execute_typo_edit(page, conn, account)
                elif edit_type == "spacing":
                    result = execute_spacing_edit(page, conn, account)
                else:
                    result = execute_link_fix(page, conn, account)
                    if not result.get("success") and "No fixable" in result.get("error", ""):
                        fallback_type = "typo" if account["edit_count"] % 2 == 0 else "spacing"
                        log.info("No fixable links — falling back to %s edit for %s", fallback_type, username)
                        edit_type = fallback_type
                        if fallback_type == "typo":
                            result = execute_typo_edit(page, conn, account)
                        else:
                            result = execute_spacing_edit(page, conn, account)

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
                    # Verify the edit actually landed on Wikipedia
                    if title and title != "N/A" and not verify_edit_landed(title, username):
                        log.warning("Edit reported success but NOT found on Wikipedia — reverting to failed")
                        result["success"] = False
                        result["error"] = "Edit not confirmed on Wikipedia (silent failure)"
                        edit_id = result.get("edit_id")
                        if edit_id:
                            update_edit_status(conn, edit_id, status="failed",
                                             error_message="Edit not confirmed on Wikipedia")
                            page_id = result.get("page_id")
                            if page_id:
                                mark_page_unclaimed(conn, page_id)

                if result.get("success"):
                    log.info("Edit verified for %s on %s", username, title)
                    increment_edit_count(conn, username)

                    # Check state transition
                    updated = get_account(conn, username)
                    if should_transition_state(updated["state"], updated["edit_count"]):
                        update_account_state(conn, username, "active")
                        log.info("Account %s transitioned to active", username)
                else:
                    error = result.get("error", "Unknown error")
                    log.error("Edit failed for %s: %s", username, error)

                accounts_used.append(username)

        except Exception as exc:
            log.exception("Error processing account %s", username)
            edit_records.append({
                "account": username,
                "time": datetime.now(timezone.utc).strftime("%H:%M"),
                "edit_type": "unknown",
                "title": "N/A",
                "status": "failed",
                "revision_id": None,
                "error_message": str(exc)[:200],
            })

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
    account_details = get_all_account_summaries(conn)
    report = format_edit_report(
        edit_records, accounts_used, usage,
        account_summary=account_summary,
        fixable_count=len(fixable),
        health=health,
        account_details=account_details,
    )
    log.info("Edit report:\n%s", report)
    send_notification(report, WEBHOOK_URL)

    conn.close()
    log.info("=== Daily edit cycle complete ===")


if __name__ == "__main__":
    dry_run = "--dry" in sys.argv
    run(dry_run=dry_run)
