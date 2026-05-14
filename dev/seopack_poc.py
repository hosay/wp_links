"""POC: Automate seopack.com via Camoufox.

Step 1: Login and explore the UI — take screenshots at each step.
Step 2: Navigate to broken link reports and extract data.

Usage:
    python -m dev.seopack_poc explore    # Screenshot-based UI exploration
    python -m dev.seopack_poc audit URL  # Run broken link audit for a domain
"""

import json
import logging
import os
import random
import sys
import time

from dotenv import load_dotenv
from camoufox.sync_api import Camoufox

load_dotenv()
log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

SEOPACK_URL = os.environ.get("SEOPACK_URL", "https://seopack.org")
SCREENSHOTS_DIR = "dev/data/seopack_screenshots"


def _human_delay(min_s: float = 2.0, max_s: float = 5.0):
    time.sleep(random.uniform(min_s, max_s))


def _type_human(page, selector: str, text: str):
    """Type text with human-like per-keystroke delays."""
    page.click(selector)
    _human_delay(0.5, 1.0)
    page.type(selector, text, delay=random.uniform(50, 150))


def _screenshot(page, name: str):
    os.makedirs(SCREENSHOTS_DIR, exist_ok=True)
    path = os.path.join(SCREENSHOTS_DIR, f"{name}.png")
    page.screenshot(path=path, full_page=True)
    log.info("Screenshot saved: %s", path)
    return path


def login(page) -> bool:
    """Login to seopack.com. Returns True on success."""
    username = os.environ.get("SEOPACK_USERNAME")
    password = os.environ.get("SEOPACK_PASSWORD")
    if not username or not password:
        log.error("SEOPACK_USERNAME / SEOPACK_PASSWORD not set in .env")
        return False

    # Go directly to the login page
    login_url = f"{SEOPACK_URL}/v2/login/"
    log.info("Navigating to %s ...", login_url)
    page.goto(login_url, wait_until="networkidle")
    _screenshot(page, "01_login_page")
    _dump_page_structure(page, "01_login_page")
    _human_delay()

    # Find and fill form fields — try multiple selectors
    input_fields = page.query_selector_all("input")
    log.info("Found %d input fields", len(input_fields))
    for inp in input_fields:
        name = inp.get_attribute("name") or ""
        itype = inp.get_attribute("type") or ""
        placeholder = inp.get_attribute("placeholder") or ""
        log.info("  input: name=%s type=%s placeholder=%s", name, itype, placeholder)

    # Try common username/email selectors
    username_sel = _find_selector(page, [
        'input[name="username"]', 'input[name="email"]',
        'input[name="login"]', 'input[name="user"]',
        'input[type="email"]', 'input[type="text"]',
    ])
    password_sel = _find_selector(page, [
        'input[name="password"]', 'input[type="password"]',
    ])

    if not username_sel or not password_sel:
        log.error("Could not find login form fields")
        return False

    _type_human(page, username_sel, username)
    _human_delay(0.5, 1.5)
    _type_human(page, password_sel, password)
    _human_delay(0.5, 1.0)
    _screenshot(page, "03_login_filled")

    # Submit
    submit = _find_selector(page, [
        'button[type="submit"]', 'input[type="submit"]',
        'button:has-text("Login")', 'button:has-text("Sign")',
        'button:has-text("Log in")', 'button:has-text("Entrar")',
    ])
    if submit:
        page.click(submit)
    else:
        page.keyboard.press("Enter")
    page.wait_for_load_state("load")
    _human_delay()
    _screenshot(page, "04_after_login")

    url = page.url
    log.info("Post-login URL: %s", url)
    return True  # We'll verify from screenshots


def _find_selector(page, selectors: list[str]) -> str | None:
    """Return the first selector that matches an element on the page."""
    for sel in selectors:
        try:
            el = page.query_selector(sel)
            if el:
                return sel
        except Exception:
            continue
    return None


def _dump_page_structure(page, name: str):
    """Save all links and form elements for analysis."""
    os.makedirs(SCREENSHOTS_DIR, exist_ok=True)
    data = {
        "url": page.url,
        "title": page.title(),
        "links": [],
        "inputs": [],
        "buttons": [],
    }
    for a in page.query_selector_all("a[href]"):
        href = a.get_attribute("href") or ""
        text = (a.inner_text() or "").strip()[:100]
        if text:
            data["links"].append({"text": text, "href": href})
    for inp in page.query_selector_all("input"):
        data["inputs"].append({
            "name": inp.get_attribute("name"),
            "type": inp.get_attribute("type"),
            "placeholder": inp.get_attribute("placeholder"),
        })
    for btn in page.query_selector_all("button"):
        data["buttons"].append({
            "text": (btn.inner_text() or "").strip()[:100],
            "type": btn.get_attribute("type"),
        })
    path = os.path.join(SCREENSHOTS_DIR, f"{name}_structure.json")
    with open(path, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    log.info("Page structure saved: %s (%d links, %d inputs)", path, len(data["links"]), len(data["inputs"]))


def explore(page):
    """Take screenshots of the main dashboard and navigation to understand the UI."""
    _screenshot(page, "04_dashboard")
    _dump_page_structure(page, "04_dashboard")

    # Look for navigation links
    links = page.query_selector_all("a[href]")
    nav_items = []
    for link in links:
        href = link.get_attribute("href") or ""
        text = (link.inner_text() or "").strip()
        if text and len(text) < 50:
            nav_items.append({"text": text, "href": href})

    log.info("Found %d navigation links", len(nav_items))
    nav_path = os.path.join(SCREENSHOTS_DIR, "navigation.json")
    with open(nav_path, "w") as f:
        json.dump(nav_items, f, indent=2, ensure_ascii=False)
    log.info("Navigation structure saved: %s", nav_path)

    for link in nav_items:
        text_lower = link["text"].lower()
        if any(kw in text_lower for kw in ["semrush", "backlink", "audit", "broken", "link"]):
            log.info("Potential tool link: %s -> %s", link["text"], link["href"])

    # Navigate to SemRush tool page
    log.info("Navigating to SemRush tool page...")
    semrush_link = page.query_selector('a[href="SemRush"]')
    if semrush_link:
        semrush_link.click()
        page.wait_for_load_state("load")
        _human_delay(3, 6)
        _screenshot(page, "05_semrush_page")
        _dump_page_structure(page, "05_semrush_page")
        log.info("SemRush page URL: %s", page.url)

        # Check for iframes — seopack likely embeds tools in iframes
        iframes = page.query_selector_all("iframe")
        log.info("Found %d iframes on SemRush page", len(iframes))
        for i, iframe in enumerate(iframes):
            src = iframe.get_attribute("src") or ""
            log.info("  iframe[%d] src: %s", i, src[:200])

    # Click "ACCESS SEMRUSH 01" button — it opens a new tab
    access_btns = page.query_selector_all('a:has-text("ACCESS SEMRUSH"), a:has-text("Access Semrush")')
    if not access_btns:
        # Try broader selectors
        access_btns = page.query_selector_all('a.btn, a.button')
    log.info("Found %d SemRush access buttons", len(access_btns))

    if access_btns:
        # Listen for new page (popup/tab)
        context = page.context
        with context.expect_page() as new_page_info:
            access_btns[0].click()
        new_page = new_page_info.value
        new_page.wait_for_load_state("load")
        _human_delay(5, 8)
        log.info("SemRush opened at: %s", new_page.url)
        _screenshot(new_page, "07_semrush_actual")
        _dump_page_structure(new_page, "07_semrush_actual")
        new_page.close()

    # Go back and check Majestic
    page.go_back()
    page.wait_for_load_state("load")
    _human_delay(2, 4)

    log.info("Navigating to Majestic tool page...")
    majestic_link = page.query_selector('a[href="Majestic"]')
    if majestic_link:
        majestic_link.click()
        page.wait_for_load_state("load")
        _human_delay(3, 6)
        _screenshot(page, "08_majestic_page")
        _dump_page_structure(page, "08_majestic_page")
        log.info("Majestic page URL: %s", page.url)

        # Click Access Majestic button
        majestic_btns = page.query_selector_all('a:has-text("Access Majestic")')
        log.info("Found %d Majestic access buttons", len(majestic_btns))
        if majestic_btns:
            with page.context.expect_page() as new_page_info:
                majestic_btns[0].click()
            new_page = new_page_info.value
            new_page.wait_for_load_state("load")
            _human_delay(5, 8)
            log.info("Majestic opened at: %s", new_page.url)
            _screenshot(new_page, "09_majestic_actual")
            _dump_page_structure(new_page, "09_majestic_actual")
            new_page.close()


def run_explore():
    """Launch Camoufox and explore seopack.com."""
    with Camoufox(headless=True) as browser:
        page = browser.new_page()
        if login(page):
            log.info("Login successful!")
            explore(page)
        else:
            log.error("Login failed — check credentials and screenshots")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "explore"
    if cmd == "explore":
        run_explore()
    else:
        print(f"Unknown command: {cmd}")
        print("Usage: python -m dev.seopack_poc explore")
