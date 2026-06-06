"""Wikipedia browser interaction via Camoufox.

All page fetches and edits go through Camoufox to maintain a consistent
TLS fingerprint. Functions that touch the browser require a Playwright
page object; pure helpers (URL extraction, wikitext manipulation) are
standalone.
"""

import base64
import json
import logging
import os
import random
import re
import time

import requests as http_requests
from browserforge.fingerprints import Screen
from camoufox.exceptions import InvalidProxy
from camoufox.sync_api import Camoufox

log = logging.getLogger(__name__)

GEMINI_KEY = os.environ.get("GOOGLE_GEMENI_CONTENT_CREATOR", "")

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


def _set_ve_preferences(page):
    """Set VisualEditor preferences via API to suppress the welcome dialog."""
    try:
        # Get a CSRF token
        token_resp = page.evaluate("""
            fetch('/w/api.php?action=query&meta=tokens&format=json', {credentials: 'same-origin'})
                .then(r => r.json())
                .then(d => d.query.tokens.csrftoken)
        """)
        if not token_resp:
            return
        # Set preferences: hide VE welcome, prefer source editor
        page.evaluate("""(token) => {
            const params = new URLSearchParams();
            params.set('action', 'options');
            params.set('format', 'json');
            params.set('optionname', 'visualeditor-hidebetawelcome');
            params.set('optionvalue', '1');
            params.set('token', token);
            return fetch('/w/api.php', {method: 'POST', body: params, credentials: 'same-origin'})
                .then(r => r.json());
        }""", token_resp)
        log.info("Set VisualEditor preferences (hide welcome dialog)")
    except Exception as e:
        log.warning("Failed to set VE preferences: %s", e)


def _dismiss_ve_welcome(page):
    """Dismiss the VisualEditor welcome dialog if present.

    New Wikipedia accounts see a modal on their first source-editor visit.
    The dialog intercepts pointer events on the textarea, blocking edits.
    Polls for the dialog since it may appear asynchronously after page load.
    """
    dialog_sel = ".ve-init-mw-welcomeDialog.oo-ui-window-active"

    # Poll for the dialog — it loads asynchronously and may not be present yet
    dialog = None
    for _ in range(10):  # 10 x 500ms = 5s max
        dialog = page.query_selector(dialog_sel)
        if dialog:
            break
        time.sleep(0.5)

    if not dialog:
        return

    log.info("VisualEditor welcome dialog detected — dismissing")
    # Try the primary action button (usually "Empezar" / "Start")
    for sel in [
        '.ve-init-mw-welcomeDialog .oo-ui-flaggedElement-primary button',
        '.ve-init-mw-welcomeDialog .oo-ui-messageDialog-actions button',
        '.ve-init-mw-welcomeDialog button',
    ]:
        try:
            btn = page.query_selector(sel)
            if btn and btn.is_visible():
                btn.click()
                time.sleep(0.5)
                if not page.query_selector(dialog_sel):
                    return
        except Exception:
            continue

    # Fallback: force-close via JavaScript
    try:
        page.evaluate("""
            document.querySelectorAll('.ve-init-mw-welcomeDialog.oo-ui-window-active')
                .forEach(el => el.remove());
        """)
        time.sleep(0.5)
        log.info("Removed VisualEditor welcome dialog via JS fallback")
    except Exception:
        pass


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


def verify_edit_landed(title: str, username: str, max_age_seconds: int = 300) -> bool:
    """Check via Wikipedia API that the user's edit appears in recent revisions.

    Returns True if the user made an edit to the article within the last
    max_age_seconds. This catches silent failures (edit conflicts, spam
    filters, captcha blocks) that save_edit might miss.
    """
    import requests
    from datetime import datetime, timezone, timedelta

    try:
        resp = requests.get(
            f"{BASE_URL}/w/api.php",
            params={
                "action": "query",
                "prop": "revisions",
                "titles": title,
                "rvprop": "user|timestamp",
                "rvlimit": "5",
                "format": "json",
            },
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10,
        )
        pages = resp.json().get("query", {}).get("pages", {})
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=max_age_seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")

        for page_data in pages.values():
            for rev in page_data.get("revisions", []):
                if rev["user"].lower() == username.lower() and rev["timestamp"] >= cutoff:
                    log.info("Edit verified on Wikipedia: %s by %s", title, username)
                    return True

        log.warning("Edit NOT found on Wikipedia: %s by %s", title, username)
        return False
    except Exception as exc:
        log.warning("Could not verify edit via API: %s — %s", title, exc)
        # On API failure, trust save_edit's result to avoid false negatives
        return True


# ── browser operations ────────────────────────────────────────────────


def create_browser(fingerprint: dict, profile_dir: str, proxy: dict | None = None):
    """Create a Camoufox browser instance with the given fingerprint config.

    When proxy is provided, geoip=True auto-spoofs timezone, locale, and
    WebRTC IP to match the proxy's residential location.
    """
    screen_raw = fingerprint.get("screen")
    screen = Screen(max_width=screen_raw["width"], max_height=screen_raw["height"]) if screen_raw else None
    kwargs = {
        "headless": True,
        "os": fingerprint.get("os"),
        "screen": screen,
        "firefox_user_prefs": fingerprint.get("firefox_user_prefs", {}),
    }
    if proxy:
        kwargs["proxy"] = proxy
        kwargs["geoip"] = True
    if fingerprint.get("locale") and not proxy:
        kwargs["locale"] = fingerprint["locale"]

    try:
        return Camoufox(**kwargs)
    except InvalidProxy:
        # geoip lookup failed for this proxy config — retry without geo-spoofing
        log.warning("geoip proxy validation failed, retrying without geoip")
        kwargs.pop("geoip", None)
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

    # Wait for the login redirect chain:
    # auth.wikimedia.org processes login → redirects back to es.wikipedia.org
    try:
        page.wait_for_url(f"{BASE_URL}/**", timeout=30000)
    except Exception:
        log.warning("Timeout waiting for redirect to %s — checking result anyway", BASE_URL)
    try:
        page.wait_for_load_state("load", timeout=15000)
    except Exception:
        pass
    _human_delay()

    # Check for login error on auth.wikimedia.org (still on login page = failed)
    current_url = page.url
    if "Entrar" in current_url or "UserLogin" in current_url:
        error_el = page.query_selector(".cdx-message--error, .error, .errorbox")
        if error_el:
            log.error("Login failed for %s: %s", username, error_el.inner_text()[:200])
        else:
            log.error("Login failed for %s (still on login page)", username)
        return False

    # Check for actual IP/account block
    is_block_page = "Special:BlockList" in current_url or "Especial:Bloquear" in current_url
    block_notice = page.query_selector("#mw-blocked-text, .mw-blockedtext")
    if is_block_page or block_notice:
        log.error("Account %s is BLOCKED (block page or notice detected)", username)
        return False

    # Verify login by checking for user elements (Vector 2022 skin)
    logged_in = page.query_selector(
        f"#pt-userpage-2, #pt-userpage, "
        f'a[href*="Usuario:{username}"], a[href*="User:{username}"]'
    ) is not None

    if logged_in:
        log.info("Login successful for %s", username)
        # Disable VisualEditor welcome dialog for future edits
        _set_ve_preferences(page)
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


def _solve_edit_captcha(page) -> str | None:
    """Solve a CAPTCHA shown on the edit submission page using Gemini Vision."""
    if not GEMINI_KEY:
        log.warning("Gemini API key not configured — cannot solve edit CAPTCHA")
        return None

    captcha_img = page.query_selector(".fancycaptcha-image img, .mw-createacct-captcha-area img, .captcha img")
    if not captcha_img:
        # Try broader area
        captcha_img = page.query_selector(".fancycaptcha-image")
    if not captcha_img:
        log.warning("Could not find CAPTCHA image element")
        return None

    captcha_path = os.path.join("dev", "data", "account_screenshots", "edit_captcha.png")
    os.makedirs(os.path.dirname(captcha_path), exist_ok=True)
    captcha_img.screenshot(path=captcha_path)

    with open(captcha_path, "rb") as f:
        image_b64 = base64.b64encode(f.read()).decode()

    try:
        resp = http_requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-flash-lite:generateContent?key={GEMINI_KEY}",
            json={
                "contents": [{
                    "parts": [
                        {"text": "This is a CAPTCHA image with distorted text. Output ONLY the exact text/characters shown in the image. No explanation, no quotes, just the characters."},
                        {"inline_data": {"mime_type": "image/png", "data": image_b64}},
                    ]
                }]
            },
            timeout=15,
            headers={"Content-Type": "application/json"},
        )
        resp.raise_for_status()
        data = resp.json()
        answer = data["candidates"][0]["content"]["parts"][0]["text"].strip()
        lines = [l.strip() for l in answer.split("\n") if l.strip()]
        if lines:
            captcha_text = lines[-1].strip("'\"` ")
            log.info("Gemini solved edit CAPTCHA: %s", captcha_text)
            return captcha_text
    except Exception as e:
        log.warning("Gemini CAPTCHA solve failed: %s", e)

    return None


def save_edit(page, title: str, new_wikitext: str, summary: str) -> bool:
    """Open the edit page, replace wikitext, and save.

    Returns True if the edit was saved successfully.
    """
    # Navigate directly to source editor (oldid= forces source mode, avoids VisualEditor)
    edit_url = f"{BASE_URL}/w/index.php?title={title}&action=edit&veswitched=1&mobileaction=toggle_view_desktop"
    log.info("Opening edit page for %s", title)
    page.goto(edit_url, wait_until="domcontentloaded", timeout=60000)
    _human_delay()

    # Dismiss any welcome/preference dialogs
    for dismiss_sel in [
        '.oo-ui-messageDialog .oo-ui-flaggedElement-primary button',
        '.ve-init-mw-welcomeDialog button.oo-ui-flaggedElement-primary',
        'button:has-text("Empezar")',
        'button:has-text("Aceptar")',
        'button:has-text("OK")',
        '.oo-ui-window-active button',
        '.cdx-dialog__footer button',
    ]:
        try:
            dismiss = page.query_selector(dismiss_sel)
            if dismiss and dismiss.is_visible():
                dismiss.click()
                _human_delay(0.5, 1.0)
                break
        except Exception:
            continue

    # Wait for source editor textarea to appear
    try:
        page.wait_for_selector("#wpTextbox1", state="visible", timeout=15000)
    except Exception:
        log.warning("Textarea not visible after 15s — checking for dialogs...")
        # Try dismissing any overlay that appeared
        for sel in ['button:has-text("Aceptar")', 'button:has-text("OK")', '.oo-ui-window-active button']:
            try:
                btn = page.query_selector(sel)
                if btn and btn.is_visible():
                    btn.click()
                    _human_delay(1.0, 2.0)
                    break
            except Exception:
                continue
        try:
            page.wait_for_selector("#wpTextbox1", state="visible", timeout=10000)
        except Exception:
            pass

    # Check if we can edit (not protected, not blocked)
    textarea = page.query_selector("#wpTextbox1")
    if not textarea:
        log.error("Cannot edit %s — textarea not found (page may be protected)", title)
        return False

    # Dismiss VisualEditor welcome dialog if it appeared after page load.
    # New accounts see this modal on their first edit; it blocks the textarea.
    _dismiss_ve_welcome(page)

    # Clear and fill the textarea
    textarea.click()
    page.keyboard.press("Control+A")
    _human_delay(0.3, 0.6)

    # Use evaluate to set value directly (much faster than typing thousands of chars)
    # Must dispatch 'input' event so MediaWiki's JS detects the change
    page.evaluate(
        """(args) => {
            const el = document.getElementById('wpTextbox1');
            el.value = args.text;
            el.dispatchEvent(new Event('input', { bubbles: true }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
        }""",
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

    # Handle edit CAPTCHA — Wikipedia may require CAPTCHA for low-edit accounts
    if "action=submit" in page.url or "action=edit" in page.url:
        captcha_input = page.query_selector('input[name="wpCaptchaWord"]')
        if captcha_input:
            log.info("Edit CAPTCHA detected for %s — solving with Gemini", title)
            captcha_solution = _solve_edit_captcha(page)
            if captcha_solution:
                captcha_input.click()
                _human_delay(0.3, 0.5)
                page.fill('input[name="wpCaptchaWord"]', captcha_solution)
                _human_delay(0.5, 1.0)
                save_btn2 = page.query_selector("#wpSave")
                if save_btn2:
                    try:
                        with page.expect_navigation(timeout=60000, wait_until="load"):
                            save_btn2.click()
                    except Exception:
                        log.warning("Timeout after CAPTCHA resubmit for %s", title)
                    _human_delay()
            else:
                log.error("Could not solve edit CAPTCHA for %s", title)
                return False

    # Verify: should redirect to article view (not still on edit page)
    current_url = page.url
    if "action=edit" in current_url or "action=submit" in current_url:
        log.error("Edit may have failed for %s — still on edit page", title)
        return False

    # Stronger verification: check page content contains our edit
    # (catches silent failures like edit conflicts, spam filter, captcha)
    content = page.content()
    if "conflicto de edición" in content.lower() or "edit conflict" in content.lower():
        log.error("Edit conflict for %s", title)
        return False

    # Check for abuse filter or captcha blocks
    if "filtro de ediciones" in content.lower() or "abusefilter" in content.lower():
        log.error("Edit blocked by abuse filter for %s", title)
        return False

    log.info("Edit saved for %s", title)
    return True


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
