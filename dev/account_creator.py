"""Wikipedia account creator via Camoufox + residential proxies.

Creates accounts one at a time with unique fingerprints and residential
proxy IPs (Rayobyte). Uses claude -p to solve CAPTCHAs.

Usage:
    python -m dev.account_creator --create-all      # create all accounts from accounts.json
    python -m dev.account_creator --create <username> # create one specific account
    python -m dev.account_creator --explore
"""

import json
import logging
import os
import random
import sys
import time

import requests as http_requests
from dotenv import load_dotenv
from camoufox.sync_api import Camoufox

from dev.db import init_db, update_account_state
from dev.fingerprint import generate_fingerprint

load_dotenv()
log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

BASE_URL = "https://es.wikipedia.org"
SCREENSHOTS_DIR = "dev/data/account_screenshots"
ACCOUNTS_FILE = os.path.join(os.path.dirname(__file__), "data", "accounts.json")
WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")

# Rayobyte residential proxy — geo params appended to password
PROXY_HOST = os.environ.get("RAYOBYTE_PROXY_HOST", "la.residential.rayobyte.com")
PROXY_PORT = os.environ.get("RAYOBYTE_PROXY_PORT", "8000")
PROXY_USER = os.environ.get("RAYOBYTE_PROXY_USER", "")
PROXY_PASS = os.environ.get("RAYOBYTE_PROXY_PASS", "")


def build_proxy(proxy_config: dict, session_id: str = "") -> dict:
    """Build a Rayobyte proxy dict for Camoufox.

    Geo params (country, region, city) are appended to the password.
    An optional session_id pins to a sticky residential IP.

    proxy_config keys: country (required), region (optional), city (optional)
    """
    password = f"{PROXY_PASS}-country-{proxy_config['country']}"
    if proxy_config.get("region"):
        password += f"-region-{proxy_config['region']}"
    if proxy_config.get("city"):
        password += f"-city-{proxy_config['city']}"
    if session_id:
        password += f"-session-{session_id}"
    return {
        "server": f"http://{PROXY_HOST}:{PROXY_PORT}",
        "username": PROXY_USER,
        "password": password,
    }


def _human_delay(min_s: float = 2.0, max_s: float = 5.0):
    time.sleep(random.uniform(min_s, max_s))


def _type_human(page, selector: str, text: str):
    page.click(selector)
    _human_delay(0.3, 0.8)
    page.type(selector, text, delay=random.uniform(60, 140))


def _screenshot(page, name: str) -> str:
    os.makedirs(SCREENSHOTS_DIR, exist_ok=True)
    path = os.path.join(SCREENSHOTS_DIR, f"{name}.png")
    page.screenshot(path=path, full_page=True)
    log.info("Screenshot: %s", path)
    return path


def _send_slack_image(image_path: str, message: str):
    """Send a message with image to Slack via webhook."""
    if not WEBHOOK_URL:
        log.warning("No Slack webhook — can't send CAPTCHA")
        return
    payload = {"text": message}
    try:
        http_requests.post(
            WEBHOOK_URL,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=10,
        )
    except Exception as e:
        log.warning("Slack send failed: %s", e)


def _extract_captcha_image(page) -> str | None:
    """Extract the CAPTCHA image and save it. Returns path or None."""
    captcha_img = page.query_selector(".fancycaptcha-image img, #mw-createacct-captcha-area img")
    if captcha_img:
        os.makedirs(SCREENSHOTS_DIR, exist_ok=True)
        path = os.path.join(SCREENSHOTS_DIR, "captcha.png")
        captcha_img.screenshot(path=path)
        return path

    # Try broader selector — capture the whole captcha area
    captcha_area = page.query_selector("#mw-createacct-captcha-area, .cdx-field:has(#captchaWord)")
    if captcha_area:
        os.makedirs(SCREENSHOTS_DIR, exist_ok=True)
        path = os.path.join(SCREENSHOTS_DIR, "captcha.png")
        captcha_area.screenshot(path=path)
        return path

    return None


def create_account(username: str, password: str, proxy_config: dict) -> bool:
    """Create a Wikipedia account using the account's residential proxy.

    Returns True if successful. Uses Gemini Vision for CAPTCHA solving.
    The session_id is based on username for a consistent sticky IP across retries.
    Tries proxy with decreasing geo specificity if connection fails.
    """
    fp = generate_fingerprint(username)
    log.info("Creating account: %s (proxy: %s)", username, proxy_config)

    # Build proxy fallbacks: city → region → country
    proxy_attempts = []
    if proxy_config.get("city"):
        proxy_attempts.append(("full", build_proxy(proxy_config, session_id=username)))
    if proxy_config.get("region"):
        region_only = {k: v for k, v in proxy_config.items() if k != "city"}
        proxy_attempts.append(("region", build_proxy(region_only, session_id=username)))
    country_only = {"country": proxy_config["country"]}
    proxy_attempts.append(("country", build_proxy(country_only, session_id=username)))

    proxy = None
    for label, candidate_proxy in proxy_attempts:
        try:
            test_url = f"http://{candidate_proxy['username']}:{candidate_proxy['password']}@{PROXY_HOST}:{PROXY_PORT}"
            http_requests.get("http://httpbin.org/ip",
                              proxies={"http": test_url, "https": test_url}, timeout=8)
            proxy = candidate_proxy
            log.info("Proxy validated for account creation (%s): %s", label, proxy_config.get("country"))
            break
        except Exception:
            log.warning("Proxy failed for account creation (%s) — trying next", label)
            continue

    if proxy is None:
        log.error("All proxy fallbacks failed for account creation: %s", username)
        return False

    try:
        prefs = dict(fp.get("firefox_user_prefs", {}))
        prefs["security.cert_pinning.enforcement_level"] = 0

        with Camoufox(
            headless=True,
            os=fp.get("os"),
            firefox_user_prefs=prefs,
            proxy=proxy,
        ) as browser:
            ctx = browser.new_context(ignore_https_errors=True)
            page = ctx.new_page()

            reg_url = f"{BASE_URL}/w/index.php?title=Especial:Crear_una_cuenta&returnto=Portada"
            page.goto(reg_url, wait_until="networkidle")
            _human_delay()

            # Check if IP is blocked before filling the form
            pre_content = page.content().lower()
            if "bloqueado" in pre_content or "blocked" in pre_content:
                log.error("IP is BLOCKED on Wikipedia — proxy %s unusable for account creation",
                         proxy_config)
                _screenshot(page, f"create_{username}_BLOCKED")
                return False

            # Fill form fields (auth.wikimedia.org uses placeholder-based fields)
            _type_human(page, 'input[placeholder*="nombre de usuario"], input[name="wpName"]', username)
            _human_delay(0.5, 1.5)
            _type_human(page, 'input[placeholder*="contraseña"]:not([placeholder*="nuevo"]):not([placeholder*="Introduce"]), input[name="wpPassword"]', password)
            _human_delay(0.5, 1.0)
            _type_human(page, 'input[placeholder*="nuevo"], input[placeholder*="Introduce"], input[name="retype"]', password)
            _human_delay(0.5, 1.0)

            _screenshot(page, f"create_{username}_01_filled")

            # Handle CAPTCHA
            captcha_field = page.query_selector('input[name="captchaWord"], input[placeholder*="texto que ves"]')
            if captcha_field:
                log.info("CAPTCHA detected — extracting image...")
                captcha_path = _extract_captcha_image(page)
                if not captcha_path:
                    # Fallback: screenshot the whole page
                    captcha_path = _screenshot(page, f"create_{username}_02_captcha_full")

                # Try to read the CAPTCHA myself from the image
                captcha_text = _attempt_read_captcha(page)
                if captcha_text:
                    log.info("CAPTCHA read attempt: %s", captcha_text)
                    _type_human(page, 'input[name="captchaWord"], input[placeholder*="texto que ves"]', captcha_text)
                else:
                    # Send to Slack for help
                    _send_slack_image(
                        captcha_path,
                        f":key: *Wikipedia CAPTCHA* for account `{username}`\n"
                        f"Please reply with the CAPTCHA text. Screenshot saved at: `{captcha_path}`"
                    )
                    log.warning("Sent CAPTCHA to Slack. Cannot solve automatically — skipping %s", username)
                    return False

            _human_delay(0.5, 1.0)
            _screenshot(page, f"create_{username}_03_before_submit")

            # Submit
            submit = page.query_selector('button:has-text("Crea tu cuenta"), button:has-text("Crear tu cuenta"), button[name="wpCreateaccount"]')
            if submit:
                submit.click()
            else:
                log.warning("No submit button found — trying Enter key")
                page.keyboard.press("Enter")

            page.wait_for_load_state("networkidle")
            _human_delay()
            _screenshot(page, f"create_{username}_04_result")

            # Check result
            content = page.content().lower()
            if "bienvenido" in content or "bienveni" in content:
                log.info("Account %s created successfully!", username)
                _send_slack_image("", f":white_check_mark: Wikipedia account `{username}` created successfully!")
                # Activate account in DB so orchestrator can pick it up
                try:
                    db_path = os.path.join(os.path.dirname(__file__), "wp_links.db")
                    conn = init_db(db_path)
                    update_account_state(conn, username, "warmup")
                    conn.close()
                    log.info("Account %s state set to warmup in DB", username)
                except Exception as db_err:
                    log.warning("Could not update DB state for %s: %s", username, db_err)
                return True

            if "ya está registrado" in content or "already in use" in content:
                log.warning("Username %s already taken", username)
                return False

            if "captcha" in content and "incorrecto" in content:
                log.warning("CAPTCHA was incorrect for %s", username)
                return False

            if "bloqueado" in content or "blocked" in content:
                log.error("IP is BLOCKED on Wikipedia for %s — proxy %s", username, proxy_config)
                _send_slack_image("", f":no_entry: IP blocked for `{username}` on proxy `{proxy_config}`")
                return False

            # Check for error messages
            error_el = page.query_selector(".error, .errorbox, .cdx-message--error")
            if error_el:
                error_text = error_el.inner_text()
                log.error("Registration error for %s: %s", username, error_text[:200])
                return False

            log.warning("Unclear result for %s — check screenshots", username)
            return False

    except Exception as e:
        log.error("Account creation failed for %s: %s", username, e)
        return False


def _attempt_read_captcha(page) -> str | None:
    """Use Gemini Vision API to read the CAPTCHA image."""
    import base64

    GEMINI_KEY = os.environ.get("GOOGLE_GEMENI_CONTENT_CREATOR", "")
    if not GEMINI_KEY:
        log.warning("Gemini API key not configured — cannot solve CAPTCHA")
        return None

    # Save CAPTCHA image
    captcha_img = page.query_selector(".fancycaptcha-image img, .mw-createacct-captcha-area img")
    if not captcha_img:
        # Try broader area
        captcha_area = page.query_selector(".fancycaptcha-image")
        if captcha_area:
            captcha_img = captcha_area
        else:
            return None

    os.makedirs(SCREENSHOTS_DIR, exist_ok=True)
    captcha_path = os.path.abspath(os.path.join(SCREENSHOTS_DIR, "current_captcha.png"))
    captcha_img.screenshot(path=captcha_path)
    log.info("CAPTCHA image saved: %s", captcha_path)

    # Read image as base64
    with open(captcha_path, "rb") as f:
        image_b64 = base64.b64encode(f.read()).decode()

    # Call Gemini Vision API
    try:
        resp = http_requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={GEMINI_KEY}",
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
        # Clean up — take last line, strip quotes
        lines = [l.strip() for l in answer.split("\n") if l.strip()]
        if lines:
            captcha_text = lines[-1].strip("'\"` ")
            log.info("Gemini read CAPTCHA as: %s", captcha_text)
            return captcha_text
    except Exception as e:
        log.warning("Gemini CAPTCHA read failed: %s", e)

    return None


def create_all_accounts():
    """Create all accounts from accounts.json, one at a time."""
    with open(ACCOUNTS_FILE) as f:
        accounts = json.load(f)

    log.info("Creating %d accounts...", len(accounts))
    created = 0
    failed = 0

    for i, acct in enumerate(accounts):
        username = acct["username"]
        password = acct["password"]
        proxy_config = acct["proxy"]

        log.info("=== Account %d/%d: %s ===", i + 1, len(accounts), username)
        success = create_account(username, password, proxy_config)

        if success:
            created += 1
        else:
            failed += 1

        # Delay between accounts (2-5 min to avoid rate limiting)
        if i < len(accounts) - 1:
            delay = random.uniform(120, 300)
            log.info("Waiting %.0f seconds before next account...", delay)
            time.sleep(delay)

    log.info("Done: %d created, %d failed out of %d", created, failed, len(accounts))
    _send_slack_image(
        "",
        f":chart_with_upwards_trend: *Account creation complete*\n"
        f"Created: {created} | Failed: {failed} | Total: {len(accounts)}"
    )


def explore_registration():
    """Screenshot the registration page via residential proxy."""
    proxy_config = {"country": "ES"}
    proxy = build_proxy(proxy_config, session_id=f"explore_{random.randint(1000,9999)}")
    fp = generate_fingerprint("explore_reg")
    with Camoufox(
        headless=True,
        geoip=True,
        proxy=proxy,
        os=fp.get("os"),
        firefox_user_prefs=fp.get("firefox_user_prefs", {}),
    ) as browser:
        page = browser.new_page()
        reg_url = f"{BASE_URL}/w/index.php?title=Especial:Crear_una_cuenta&returnto=Portada"
        log.info("Navigating to registration: %s", reg_url)
        page.goto(reg_url, wait_until="networkidle")
        _human_delay()
        _screenshot(page, "01_registration_page")

        inputs = page.query_selector_all("input")
        log.info("Found %d input fields:", len(inputs))
        for inp in inputs:
            name = inp.get_attribute("name") or ""
            itype = inp.get_attribute("type") or ""
            log.info("  name=%s type=%s", name, itype)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage:")
        print("  python -m dev.account_creator --explore [vpn_conf]")
        print("  python -m dev.account_creator --create-all")
        print("  python -m dev.account_creator --create <username>")
        sys.exit(1)

    cmd = sys.argv[1]
    if cmd == "--explore":
        explore_registration()
    elif cmd == "--create-all":
        create_all_accounts()
    elif cmd == "--create":
        if len(sys.argv) < 3:
            print("Need username")
            sys.exit(1)
        target = sys.argv[2]
        with open(ACCOUNTS_FILE) as f:
            accounts = json.load(f)
        acct = next((a for a in accounts if a["username"] == target), None)
        if not acct:
            print(f"Account {target} not found in accounts.json")
            sys.exit(1)
        create_account(target, acct["password"], acct["proxy"])
