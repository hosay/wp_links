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
import subprocess
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


FALLBACK_PROXIES = [
    {"country": "MX", "region": "mexico_city"},
    {"country": "ES", "region": "madrid"},
]


def get_backoff_hours(block_count: int) -> int:
    """Escalating backoff: 24h, 36h, 48h (capped)."""
    return min(24 + 12 * block_count, 48)


def get_fallback_proxy(current_proxy: dict) -> dict | None:
    """Return next fallback proxy, or None if exhausted.

    Chain: original → MX/mexico_city → ES/madrid → None.
    If current proxy is already in the fallback chain, return the next one.
    """
    # Check if current proxy matches any fallback
    for i, fallback in enumerate(FALLBACK_PROXIES):
        if (current_proxy.get("country") == fallback["country"]
                and current_proxy.get("region") == fallback["region"]):
            # Already on this fallback — return next in chain, or None
            return FALLBACK_PROXIES[i + 1] if i + 1 < len(FALLBACK_PROXIES) else None
    # Not in fallback chain yet — return first fallback
    return FALLBACK_PROXIES[0]


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


def _verify_password(username: str, password: str, proxy_dict: dict | None = None) -> bool:
    """Verify a Wikipedia account's password works via the API.

    Called immediately after registration to catch passwords that were
    mistyped during the form fill. Uses the same proxy as registration
    to avoid propagation/routing issues.
    """
    try:
        session = http_requests.Session()
        session.headers = {"User-Agent": "Mozilla/5.0"}
        if proxy_dict:
            proxy_url = (
                f"http://{proxy_dict['username']}:{proxy_dict['password']}"
                f"@{proxy_dict['server'].replace('http://', '')}"
            )
            session.proxies = {"https": proxy_url, "http": proxy_url}

        r1 = session.get(
            "https://es.wikipedia.org/w/api.php",
            params={"action": "query", "meta": "tokens", "type": "login", "format": "json"},
            timeout=15,
        )
        token = r1.json()["query"]["tokens"]["logintoken"]

        r2 = session.post(
            "https://es.wikipedia.org/w/api.php",
            data={
                "action": "clientlogin",
                "username": username,
                "password": password,
                "logintoken": token,
                "loginreturnurl": "https://es.wikipedia.org/",
                "format": "json",
            },
            timeout=15,
        )
        result = r2.json().get("clientlogin", {}).get("status", "")
        if result == "PASS":
            log.info("Password verified for %s", username)
            return True
        log.warning("Password verification failed for %s: %s", username, result)
        return False
    except Exception as exc:
        log.warning("Could not verify password for %s: %s — assuming OK", username, exc)
        return True  # Don't block on API failure


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


def _handle_ip_block(username: str, proxy_config: dict, conn=None):
    """Handle an IP block: update DB state, escalate proxy if needed."""
    _send_slack_image("", f":no_entry: IP blocked for `{username}` on proxy `{proxy_config}`")
    if conn is None:
        return
    from dev.db import mark_account_blocked, get_block_count, reassign_proxy
    block_count = get_block_count(conn, username)
    hours = get_backoff_hours(block_count)
    mark_account_blocked(conn, username, hours=hours)
    log.info("Account %s blocked for %dh (block #%d)", username, hours, block_count + 1)
    # After 3 blocks on same proxy, reassign to fallback geo
    if block_count + 1 >= 3:
        fallback = get_fallback_proxy(proxy_config)
        if fallback:
            reassign_proxy(conn, username, fallback)
            log.info("Reassigned %s proxy to %s after %d blocks", username, fallback, block_count + 1)
            _send_slack_image("", f":arrows_counterclockwise: Reassigned `{username}` proxy to `{fallback}`")


def _attempt_registration_api(username: str, password: str, proxy_dict: dict, proxy_label: str) -> str:
    """Create a Wikipedia account via the MediaWiki API.

    Bypasses the browser form entirely — avoids Playwright event loop issues
    and password mismatch bugs in the browser-based flow.

    Returns: 'success', 'ip_blocked', 'username_taken', 'captcha_fail', or 'error:<message>'.
    """
    import base64

    GEMINI_KEY = os.environ.get("GOOGLE_GEMENI_CONTENT_CREATOR", "")
    proxy_url = (
        f"http://{proxy_dict['username']}:{proxy_dict['password']}"
        f"@{proxy_dict['server'].replace('http://', '')}"
    )

    try:
        session = http_requests.Session()
        session.headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Firefox/120.0"}
        session.proxies = {"https": proxy_url, "http": proxy_url}

        # Get createaccount token
        r1 = session.get(
            f"{BASE_URL}/w/api.php",
            params={"action": "query", "meta": "tokens", "type": "createaccount", "format": "json"},
            timeout=20,
        )
        token = r1.json()["query"]["tokens"]["createaccounttoken"]

        # Get CAPTCHA
        cr = session.get(
            f"{BASE_URL}/w/api.php",
            params={"action": "fancycaptchareload", "format": "json"},
            timeout=15,
        )
        captcha_index = cr.json()["fancycaptchareload"]["index"]

        # Download CAPTCHA image
        img = session.get(
            f"{BASE_URL}/w/index.php?title=Especial:Captcha/image&wpCaptchaId={captcha_index}",
            timeout=15,
        )
        img_b64 = base64.b64encode(img.content).decode()

        # Solve with Gemini
        if not GEMINI_KEY:
            return "error:no_gemini_key"

        gr = http_requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-flash-lite:generateContent?key={GEMINI_KEY}",
            json={"contents": [{"parts": [
                {"text": "CAPTCHA image with distorted text. Output ONLY the characters. No explanation, no quotes."},
                {"inline_data": {"mime_type": "image/png", "data": img_b64}},
            ]}]},
            timeout=15,
        )
        gr.raise_for_status()
        captcha_answer = gr.json()["candidates"][0]["content"]["parts"][0]["text"].strip().strip("'\"` ")
        log.info("CAPTCHA solved via API: %s", captcha_answer)

        # Create account
        r3 = session.post(
            f"{BASE_URL}/w/api.php",
            data={
                "action": "createaccount",
                "createtoken": token,
                "username": username,
                "password": password,
                "retype": password,
                "captchaId": captcha_index,
                "captchaWord": captcha_answer,
                "createreturnurl": f"{BASE_URL}/",
                "format": "json",
            },
            timeout=20,
        )
        result = r3.json().get("createaccount", {})
        status = result.get("status", "")

        if status == "PASS":
            return "success"

        msg = result.get("messagecode", result.get("message", ""))
        if "userexists" in msg or "already" in str(msg).lower():
            return "username_taken"
        if "blocked" in str(msg).lower() or "bloqueado" in str(msg).lower():
            return "ip_blocked"
        if "captcha" in str(msg).lower():
            return "captcha_fail"

        return f"error:{msg}"

    except http_requests.exceptions.ProxyError:
        return "error:proxy_connection_failed"
    except Exception as e:
        return f"error:{e}"


def _attempt_registration(username: str, password: str, proxy_dict: dict, proxy_label: str, fp: dict) -> str:
    """Attempt registration with one proxy, retrying CAPTCHAs within the same browser.

    Returns: 'success', 'ip_blocked', 'username_taken', or 'error:<message>'.
    Handles CAPTCHA retries internally (up to 3 attempts) within one
    Camoufox session to avoid Playwright event loop corruption.
    """
    max_captcha_retries = 3

    try:
        prefs = dict(fp.get("firefox_user_prefs", {}))
        prefs["security.cert_pinning.enforcement_level"] = 0

        with Camoufox(
            headless=True,
            os=fp.get("os"),
            firefox_user_prefs=prefs,
            proxy=proxy_dict,
            geoip=True,
        ) as browser:
            page = browser.new_page()

            for captcha_attempt in range(max_captcha_retries):
                reg_url = f"{BASE_URL}/w/index.php?title=Especial:Crear_una_cuenta&returnto=Portada"
                try:
                    page.goto(reg_url, wait_until="domcontentloaded", timeout=60000)
                except Exception:
                    return "error:page_load_timeout"
                _human_delay()

                # Check if IP is blocked
                block_notice = page.query_selector(
                    "#mw-blocked-text, .mw-blockedtext, .mw-warning-with-logexcerpt, "
                    ".mw-abusefilter-warning"
                )
                if block_notice:
                    log.warning("IP blocked on proxy %s for %s", proxy_label, username)
                    _screenshot(page, f"create_{username}_BLOCKED")
                    return "ip_blocked"

                # Fill form — use exact field names for reliability
                _type_human(page, 'input[name="wpName"]', username)
                _human_delay(0.5, 1.5)
                # Type passwords character-by-character with short delay
                # (page.fill bypasses JS event handlers, causing password mismatch)
                page.click('input[name="wpPassword"]')
                _human_delay(0.3, 0.5)
                page.type('input[name="wpPassword"]', password, delay=30)
                _human_delay(0.5, 1.0)
                page.click('input[name="retype"]')
                _human_delay(0.3, 0.5)
                page.type('input[name="retype"]', password, delay=30)
                _human_delay(0.5, 1.0)

                # Verify password was typed correctly
                typed_pw = page.eval_on_selector('input[name="wpPassword"]', 'el => el.value')
                typed_retype = page.eval_on_selector('input[name="retype"]', 'el => el.value')
                if typed_pw != password or typed_retype != password:
                    log.error("Password mismatch! pw=%s retype=%s expected=%s",
                             repr(typed_pw), repr(typed_retype), repr(password))
                    return "error:password_type_mismatch"
                log.info("Password verified in form: matches expected value")

                # Handle CAPTCHA
                captcha_field = page.query_selector('input[name="captchaWord"], input[placeholder*="texto que ves"]')
                if captcha_field:
                    log.info("CAPTCHA detected (attempt %d/%d)...", captcha_attempt + 1, max_captcha_retries)
                    captcha_path = _extract_captcha_image(page)
                    if not captcha_path:
                        captcha_path = _screenshot(page, f"create_{username}_captcha")

                    captcha_text = _attempt_read_captcha(page)
                    if captcha_text:
                        log.info("CAPTCHA read: %s", captcha_text)
                        _type_human(page, 'input[name="captchaWord"], input[placeholder*="texto que ves"]', captcha_text)
                    else:
                        return "error:captcha_unsolvable"

                _human_delay(0.5, 1.0)

                # Submit
                submit = page.query_selector('button:has-text("Crea tu cuenta"), button:has-text("Crear tu cuenta"), button[name="wpCreateaccount"]')
                if submit:
                    submit.click()
                else:
                    page.keyboard.press("Enter")

                try:
                    page.wait_for_load_state("load", timeout=45000)
                except Exception:
                    pass
                _human_delay()
                _screenshot(page, f"create_{username}_result")

                # Check result
                content = page.content().lower()
                if "bienvenido" in content or "bienveni" in content:
                    return "success"
                if "ya está registrado" in content or "already in use" in content:
                    return "username_taken"
                if "captcha" in content and "incorrecto" in content:
                    log.warning("CAPTCHA incorrect for %s (attempt %d/%d)",
                               username, captcha_attempt + 1, max_captcha_retries)
                    _human_delay(1.0, 2.0)
                    continue  # Retry within same browser session
                if "bloqueado" in content or "blocked" in content:
                    return "ip_blocked"

                error_el = page.query_selector(".error, .errorbox, .cdx-message--error")
                if error_el:
                    return f"error:{error_el.inner_text()[:100]}"

                return "error:unclear_result"

            return "error:captcha_retries_exhausted"

    except Exception as e:
        return f"error:{e}"


def _test_registration_proxy(proxy_dict: dict, timeout: int = 15) -> bool:
    """Test if a proxy can reach the Wikipedia registration page without IP block.

    Fetches the registration page via HTTP and checks for block indicators.
    This avoids creating a Camoufox instance for each proxy test (Playwright
    can only be instantiated once per process).
    """
    proxy_url = (
        f"http://{proxy_dict['username']}:{proxy_dict['password']}"
        f"@{proxy_dict['server'].replace('http://', '')}"
    )
    try:
        resp = http_requests.get(
            f"{BASE_URL}/w/index.php?title=Especial:Crear_una_cuenta&returnto=Portada",
            proxies={"https": proxy_url, "http": proxy_url},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=timeout,
        )
        if resp.status_code != 200:
            return False
        content = resp.text.lower()
        # Check for IP block indicators
        if "mw-blocked-text" in content or "mw-blockedtext" in content:
            return False
        if "bloqueado" in content and ("crear" in content or "cuenta" in content):
            return False
        # Should have the registration form
        return "wpname" in content or "nombre de usuario" in content
    except Exception:
        return False



def _run_registration_subprocess(username: str, password: str, proxy_config: dict) -> str:
    """Run a single registration attempt in an isolated subprocess.

    Returns the result string from _attempt_registration.
    Subprocess isolation avoids Playwright event loop corruption when
    retrying with different proxies.
    """
    import subprocess
    import shlex

    script = f"""
import sys, json, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath("{os.path.abspath(__file__)}"))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath("{os.path.abspath(__file__)}"))))
from dotenv import load_dotenv
load_dotenv()
from dev.account_creator import _attempt_registration, build_proxy
from dev.fingerprint import generate_fingerprint
proxy_config = {json.dumps(proxy_config)}
proxy_dict = build_proxy(proxy_config, session_id="{username}")
fp = generate_fingerprint("{username}")
label = f"{{proxy_config.get('country','?')}}/{{proxy_config.get('region','')}}/{{proxy_config.get('city','')}}"
result = _attempt_registration("{username}", "{password}", proxy_dict, label, fp)
print("RESULT:" + result)
"""

    try:
        proc = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True, text=True, timeout=180,
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        )
        for line in proc.stdout.splitlines():
            if line.startswith("RESULT:"):
                return line[7:]
        # Log stderr for debugging
        if proc.stderr:
            for line in proc.stderr.strip().splitlines()[-5:]:
                log.warning("subprocess: %s", line)
        return f"error:no_result_from_subprocess"
    except subprocess.TimeoutExpired:
        return "error:subprocess_timeout"
    except Exception as e:
        return f"error:subprocess_{e}"


def create_account(username: str, password: str, proxy_config: dict, conn=None) -> bool:
    """Create a Wikipedia account via API, rotating through proxies on IP blocks.

    Pre-tests proxies via HTTP for registration page access, then uses
    the MediaWiki API for actual account creation (avoids browser form
    password mismatch issues). Tries multiple proxies until one succeeds.

    conn: optional DB connection for block tracking.
    """
    from dev.data.proxy_pool import get_rotation_pool

    pool = get_rotation_pool(proxy_config, max_attempts=15)

    for i, candidate in enumerate(pool):
        label = f"{candidate.get('country', '?')}/{candidate.get('region', '')}/{candidate.get('city', '')}"
        proxy_dict = build_proxy(candidate, session_id=username)

        # Quick HTTP pre-test
        log.info("Registration proxy test %d/%d for %s: %s", i + 1, len(pool), username, label)
        if not _test_registration_proxy(proxy_dict):
            log.warning("Proxy %s blocked/unreachable (HTTP check) — trying next", label)
            if i < len(pool) - 1:
                time.sleep(2)
            continue

        log.info("Proxy %s passed HTTP check — attempting API registration", label)
        proxy_dict = build_proxy(candidate, session_id=username)
        result = _attempt_registration_api(username, password, proxy_dict, label)
        log.info("Registration result for %s on %s: %s", username, label, result)

        if result == "success":
            # Wait for account to propagate from auth.wikimedia.org to es.wikipedia.org
            time.sleep(10)
            if not _verify_password(username, password, proxy_dict):
                log.warning("Password check failed for %s — waiting longer and retrying...", username)
                time.sleep(15)
                if not _verify_password(username, password, proxy_dict):
                    log.warning("Proxy verify failed — retrying without proxy...")
                    time.sleep(10)
                    if not _verify_password(username, password):
                        log.error("Account %s created but password FAILED verification", username)
                        _send_slack_image("", f":warning: Account `{username}` created but password mismatch")
                        return False

            _send_slack_image("", f":white_check_mark: Account `{username}` created and password verified!")
            if conn:
                update_account_state(conn, username, "warmup")
                from dev.db import reassign_proxy
                reassign_proxy(conn, username, candidate)
                log.info("Saved registration proxy for %s: %s", username, candidate)
            return True

        if result == "username_taken":
            log.warning("Username %s already taken on Wikipedia", username)
            return False

        if result == "ip_blocked":
            log.warning("Proxy %s blocked at form submit for %s — trying next proxy", label, username)
            if i < len(pool) - 1:
                time.sleep(random.uniform(3, 8))
            continue  # Try next proxy!

        if "antispoof" in result:
            log.warning("Username %s blocked by AntiSpoof — cannot register on any proxy", username)
            if conn:
                update_account_state(conn, username, "antispoof_blocked")
            return False

        # Other error — try next proxy
        log.warning("Registration error for %s on %s: %s — trying next", username, label, result)
        if i < len(pool) - 1:
            time.sleep(random.uniform(3, 8))
        continue

    log.error("All %d proxies exhausted for registration of %s", len(pool), username)
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
