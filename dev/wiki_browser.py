"""Wikipedia browser interaction via Camoufox.

All page fetches and edits go through Camoufox to maintain a consistent
TLS fingerprint. Functions that touch the browser require a Playwright
page object; pure helpers (URL extraction, wikitext manipulation) are
standalone.
"""

import json
import logging
import random
import re
import time

from camoufox.sync_api import Camoufox

log = logging.getLogger(__name__)

BASE_URL = "https://es.wikipedia.org"
HUMAN_DELAY_MIN = 3.0
HUMAN_DELAY_MAX = 8.0
KEYSTROKE_DELAY_MIN = 50
KEYSTROKE_DELAY_MAX = 150


# ── helpers ───────────────────────────────────────────────────────────


def _human_delay(min_s: float = HUMAN_DELAY_MIN, max_s: float = HUMAN_DELAY_MAX):
    time.sleep(random.uniform(min_s, max_s))


def _type_human(page, selector: str, text: str):
    page.click(selector)
    _human_delay(0.3, 0.8)
    page.type(selector, text, delay=random.uniform(KEYSTROKE_DELAY_MIN, KEYSTROKE_DELAY_MAX))


# ── URL builders ──────────────────────────────────────────────────────


def build_edit_url(title: str) -> str:
    return f"{BASE_URL}/w/index.php?title={title}&action=edit&mobileaction=toggle_view_desktop"


def build_raw_url(title: str) -> str:
    return f"{BASE_URL}/w/index.php?title={title}&action=raw"


def build_page_url(title: str) -> str:
    return f"{BASE_URL}/wiki/{title}"


# ── wikitext operations ───────────────────────────────────────────────


_URL_RE = re.compile(r'https?://[^\s\]\|\}<>"]+')


def extract_external_urls(wikitext: str) -> list[str]:
    """Extract all unique external URLs from wikitext."""
    urls = _URL_RE.findall(wikitext)
    seen = set()
    unique = []
    for url in urls:
        if url not in seen:
            seen.add(url)
            unique.append(url)
    return unique


def replace_url_in_wikitext(wikitext: str, old_url: str, new_url: str) -> str:
    """Replace an exact URL in wikitext, preserving surrounding markup."""
    return wikitext.replace(old_url, new_url)


# ── browser operations ────────────────────────────────────────────────


def create_browser(fingerprint: dict, profile_dir: str, proxy: dict | None = None):
    """Create a Camoufox browser instance with the given fingerprint config."""
    kwargs = {
        "headless": True,
        "os": fingerprint.get("os"),
        "screen": fingerprint.get("screen"),
        "firefox_user_prefs": fingerprint.get("firefox_user_prefs", {}),
    }
    if proxy:
        kwargs["proxy"] = proxy
    if fingerprint.get("locale"):
        kwargs["locale"] = fingerprint["locale"]

    return Camoufox(**kwargs)


def login(page, username: str, password: str) -> bool:
    """Login to es.wikipedia.org. Returns True on success."""
    login_url = f"{BASE_URL}/w/index.php?title=Especial:Entrar&returnto=Portada"
    log.info("Logging in as %s...", username)
    page.goto(login_url, wait_until="domcontentloaded", timeout=60000)
    _human_delay()

    _type_human(page, "#wpName1", username)
    _human_delay(0.5, 1.5)
    _type_human(page, "#wpPassword1", password)
    _human_delay(0.5, 1.0)

    page.click("#wpLoginAttempt")
    try:
        page.wait_for_load_state("load", timeout=30000)
    except Exception:
        log.warning("Timeout waiting for login redirect — checking result anyway")
    _human_delay()

    # Check for block message before login verification
    content = page.content().lower()
    block_markers = ["bloqueado", "blocked", "tu cuenta ha sido bloqueada", "autoblock"]
    for marker in block_markers:
        if marker in content:
            log.error("Account %s appears to be BLOCKED (found '%s')", username, marker)
            return False

    # Verify login by checking for user menu or username on page
    logged_in = page.query_selector("#pt-userpage, .mw-userlink") is not None
    if not logged_in:
        # Alternative: check for the personal tools list with username
        pt_user = page.query_selector(f'a[href*="Usuario:{username}"], a[href*="User:{username}"]')
        logged_in = pt_user is not None

    if logged_in:
        log.info("Login successful for %s", username)
    else:
        log.error("Login failed for %s", username)
    return logged_in


def get_wikitext(page, title: str) -> str:
    """Fetch raw wikitext for a given article title."""
    url = build_raw_url(title)
    log.info("Fetching wikitext for %s", title)
    page.goto(url, wait_until="load")
    _human_delay(1.0, 3.0)

    # Raw action returns plain text in <pre> or body
    body = page.query_selector("body")
    text = body.inner_text() if body else ""
    return text


def save_edit(page, title: str, new_wikitext: str, summary: str) -> bool:
    """Open the edit page, replace wikitext, and save.

    Returns True if the edit was saved successfully.
    """
    edit_url = build_edit_url(title)
    log.info("Opening edit page for %s", title)
    page.goto(edit_url, wait_until="domcontentloaded", timeout=60000)
    _human_delay()

    # Dismiss any welcome dialogs (VisualEditor welcome, etc.)
    for dismiss_sel in [
        '.oo-ui-messageDialog .oo-ui-flaggedElement-primary button',
        '.ve-init-mw-welcomeDialog button.oo-ui-flaggedElement-primary',
        'button:has-text("Empezar")',
        'button:has-text("Aceptar")',
        '.oo-ui-window-active button',
    ]:
        try:
            dismiss = page.query_selector(dismiss_sel)
            if dismiss and dismiss.is_visible():
                dismiss.click()
                _human_delay(0.5, 1.0)
                break
        except Exception:
            continue

    # Use source editor URL to avoid VisualEditor entirely
    if "action=edit" not in page.url:
        source_url = build_edit_url(title) + "&action=edit&veswitched=1"
        page.goto(source_url, wait_until="networkidle")
        _human_delay()

    # Check if we can edit (not protected, not blocked)
    textarea = page.query_selector("#wpTextbox1")
    if not textarea:
        log.error("Cannot edit %s — textarea not found (page may be protected)", title)
        return False

    # Clear and fill the textarea
    textarea.click()
    page.keyboard.press("Control+A")
    _human_delay(0.3, 0.6)

    # Use evaluate to set value directly (much faster than typing thousands of chars)
    page.evaluate(
        "(args) => document.getElementById('wpTextbox1').value = args.text",
        {"text": new_wikitext},
    )
    _human_delay(0.5, 1.0)

    # Fill edit summary
    summary_box = page.query_selector("#wpSummary")
    if summary_box:
        summary_box.click()
        _human_delay(0.3, 0.5)
        page.type("#wpSummary", summary,
                  delay=random.uniform(KEYSTROKE_DELAY_MIN, KEYSTROKE_DELAY_MAX))
    _human_delay(1.0, 2.0)

    # Check "minor edit" box if available
    minor_box = page.query_selector("#wpMinoredit")
    if minor_box:
        minor_box.check()
        _human_delay(0.3, 0.5)

    # Click save
    save_btn = page.query_selector("#wpSave")
    if not save_btn:
        log.error("Save button not found for %s", title)
        return False

    # Use expect_navigation to handle the post-save redirect
    try:
        with page.expect_navigation(timeout=60000, wait_until="load"):
            save_btn.click()
    except Exception:
        log.warning("Timeout waiting for navigation after save — checking result anyway")
    _human_delay()

    # Verify: should redirect to article view (not still on edit page)
    current_url = page.url
    success = "action=edit" not in current_url
    if success:
        log.info("Edit saved for %s", title)
    else:
        log.error("Edit may have failed for %s — still on edit page", title)
    return success


def get_random_article_title(page) -> str | None:
    """Navigate to a random article on es.wikipedia.org and return its title."""
    log.info("Getting random article...")
    page.goto(f"{BASE_URL}/wiki/Especial:Aleatoria", wait_until="load")
    _human_delay()

    # Extract title from the heading
    heading = page.query_selector("#firstHeading")
    if heading:
        title = heading.inner_text().strip()
        log.info("Random article: %s", title)
        return title
    return None
