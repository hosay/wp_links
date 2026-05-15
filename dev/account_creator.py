"""Wikipedia account creator via Camoufox + residential proxies.

Creates accounts one at a time with unique fingerprints and residential
proxy IPs (Bright Data). Uses claude -p to solve CAPTCHAs.

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

from dev.fingerprint import generate_fingerprint

load_dotenv()
log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

BASE_URL = "https://es.wikipedia.org"
SCREENSHOTS_DIR = "dev/data/account_screenshots"
ACCOUNTS_FILE = os.path.join(os.path.dirname(__file__), "data", "accounts.json")
VPN_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "wireguard_confs")
WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")

# Bright Data residential proxy
BRD_PROXY_HOST = os.environ.get("BRD_PROXY_HOST", "brd.superproxy.io")
BRD_PROXY_PORT = os.environ.get("BRD_PROXY_PORT", "33335")
BRD_PROXY_USER = os.environ.get("BRD_PROXY_USER", "")
BRD_PROXY_PASS = os.environ.get("BRD_PROXY_PASS", "")

# Country codes matching VPN config naming
COUNTRY_MAP = {
    "Chile": "cl", "Colombia": "co", "Mexico": "mx", "Spain": "es",
}


def _build_proxy(country_code: str = "es", session_id: str = "") -> dict:
    """Build a Bright Data proxy dict for Camoufox.

    Each session_id gets a sticky IP for the session duration.
    """
    user = f"{BRD_PROXY_USER}-country-{country_code}"
    if session_id:
        user += f"-session-{session_id}"
    return {
        "server": f"http://{BRD_PROXY_HOST}:{BRD_PROXY_PORT}",
        "username": user,
        "password": BRD_PROXY_PASS,
    }


def _country_from_vpn(vpn_conf: str) -> str:
    """Extract country code from VPN config filename."""
    basename = os.path.basename(vpn_conf)
    for name, code in COUNTRY_MAP.items():
        if basename.startswith(name):
            return code
    return "es"  # default to Spain


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


def create_account(username: str, password: str, vpn_conf_path: str) -> bool:
    """Create a Wikipedia account using residential proxy.

    Returns True if successful. Uses claude -p for CAPTCHA solving.
    """
    fp = generate_fingerprint(username)
    country = _country_from_vpn(vpn_conf_path)
    session_id = f"{username}_{random.randint(10000, 99999)}"
    proxy = _build_proxy(country, session_id)
    log.info("Creating account: %s (country: %s, session: %s)", username, country, session_id)

    try:
        prefs = dict(fp.get("firefox_user_prefs", {}))
        prefs["security.cert_pinning.enforcement_level"] = 0

        with Camoufox(headless=True, os=fp.get("os"), firefox_user_prefs=prefs) as browser:
            ctx = browser.new_context(proxy=proxy, ignore_https_errors=True)
            page = ctx.new_page()

            reg_url = f"{BASE_URL}/w/index.php?title=Especial:Crear_una_cuenta&returnto=Portada"
            page.goto(reg_url, wait_until="networkidle")
            _human_delay()

            # Check if IP is blocked before filling the form
            pre_content = page.content().lower()
            if "bloqueado" in pre_content or "blocked" in pre_content:
                log.error("IP is BLOCKED on Wikipedia — VPN %s unusable for account creation",
                         os.path.basename(vpn_conf_path))
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
                return True

            if "ya está registrado" in content or "already in use" in content:
                log.warning("Username %s already taken", username)
                return False

            if "captcha" in content and "incorrecto" in content:
                log.warning("CAPTCHA was incorrect for %s", username)
                return False

            if "bloqueado" in content or "blocked" in content:
                log.error("IP is BLOCKED on Wikipedia for %s — try a different VPN", username)
                _send_slack_image("", f":no_entry: IP blocked for `{username}` on VPN `{os.path.basename(vpn_conf_path)}`")
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
    """Use claude -p with vision to read the CAPTCHA image."""
    import subprocess

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

    # Use claude -p to read it
    prompt = (
        f"Read the file at {captcha_path}. "
        "This is a CAPTCHA image with distorted text. "
        "Output ONLY the text shown in the image, nothing else. "
        "No explanation, no quotes, just the exact characters."
    )

    try:
        result = subprocess.run(
            ["claude", "-p", prompt, "--allowed-tools", "Read,Bash",
             "--dangerously-skip-permissions"],
            capture_output=True, text=True, timeout=60,
            cwd="/opt/projects/wp_links",
        )
        answer = result.stdout.strip()
        # Clean up — remove any extra text, just keep the CAPTCHA
        # Take the last line if multi-line (Claude might add explanation)
        lines = [l.strip() for l in answer.split("\n") if l.strip()]
        if lines:
            captcha_text = lines[-1].strip("'\"` ")
            log.info("Claude read CAPTCHA as: %s", captcha_text)
            return captcha_text
    except subprocess.TimeoutExpired:
        log.warning("Claude CAPTCHA read timed out")
    except Exception as e:
        log.warning("Claude CAPTCHA read failed: %s", e)

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
        vpn_conf = os.path.join(VPN_DIR, acct["vpn_conf"])

        if not os.path.exists(vpn_conf):
            log.error("VPN config not found: %s — skipping %s", vpn_conf, username)
            failed += 1
            continue

        log.info("=== Account %d/%d: %s ===", i + 1, len(accounts), username)
        success = create_account(username, password, vpn_conf)

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
    proxy = _build_proxy("es", f"explore_{random.randint(1000,9999)}")
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
        vpn_path = os.path.join(VPN_DIR, acct["vpn_conf"])
        create_account(target, acct["password"], vpn_path)
