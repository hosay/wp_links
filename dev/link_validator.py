"""Broken link validation and replacement URL discovery.

For each broken URL, tries to find a replacement via:
1. HTTP redirect (domain migration)
2. Wayback Machine snapshots
3. Same-org domain check

Assigns confidence: high (same org redirect), medium (wayback/same org),
low (different source).
"""

import logging
import random
import re
import time
from urllib.parse import urlparse

log = logging.getLogger(__name__)

# Edit summary templates — varied to avoid detection patterns
_SUMMARY_TEMPLATES_REDIRECT = [
    "Corregir enlace roto: dominio migrado",
    "Actualizar enlace roto (redirección de dominio)",
    "Enlace roto corregido — nuevo dominio",
    "Corrección de enlace externo roto",
    "Reemplazar enlace roto por nueva URL",
    "Actualizar URL rota (dominio actualizado)",
]

_SUMMARY_TEMPLATES_WAYBACK = [
    "Enlace roto: reemplazado por copia archivada",
    "Enlace externo roto sustituido por versión archivada",
    "Corregir enlace roto con versión de archivo web",
    "Actualizar enlace muerto con copia de Wayback Machine",
]

_SUMMARY_TEMPLATES_GENERAL = [
    "Corregir enlace externo roto",
    "Enlace roto actualizado",
    "Corrección de referencia con enlace roto",
    "Reparar enlace externo inaccesible",
]


def extract_domain(url: str) -> str:
    """Extract the domain (netloc) from a URL."""
    return urlparse(url).netloc


def _get_base_domain(domain: str) -> str:
    """Get the base domain (last two parts) for org comparison.

    e.g., 'www.datos.gob.mx' -> 'gob.mx'
    """
    parts = domain.split(".")
    if len(parts) >= 2:
        return ".".join(parts[-2:])
    return domain


def is_same_org(url1: str, url2: str) -> bool:
    """Check if two URLs belong to the same organization.

    Considers subdomains and common gov domain patterns.
    """
    d1 = extract_domain(url1)
    d2 = extract_domain(url2)

    if d1 == d2:
        return True

    base1 = _get_base_domain(d1)
    base2 = _get_base_domain(d2)

    if base1 == base2:
        return True

    # Handle gov migration patterns: .gob.mx <-> .gobierno.mx, etc.
    # Compare the non-TLD prefix (e.g., "old" from "old.gob.mx" vs "old.gobierno.mx")
    gov_tlds = [".gob.", ".gobierno.", ".gov."]
    for tld1 in gov_tlds:
        if tld1 in d1:
            prefix1 = d1.split(tld1)[0]
            for tld2 in gov_tlds:
                if tld2 in d2:
                    prefix2 = d2.split(tld2)[0]
                    if prefix1 == prefix2:
                        return True

    return False


def build_wayback_api_url(url: str) -> str:
    """Build the Wayback Machine Availability API URL."""
    return f"https://archive.org/wayback/available?url={url}"


def parse_wayback_response(data: dict) -> dict | None:
    """Parse a Wayback Availability API response.

    Returns {"snapshot_url": ..., "timestamp": ..., "status": ...} or None.
    """
    snapshots = data.get("archived_snapshots", {})
    closest = snapshots.get("closest")
    if not closest or not closest.get("available"):
        return None
    return {
        "snapshot_url": closest["url"],
        "timestamp": closest.get("timestamp", ""),
        "status": closest.get("status", ""),
    }


def classify_confidence(original_url: str, replacement_url: str, source: str) -> str:
    """Classify the confidence level of a replacement URL.

    Returns: 'high', 'medium', or 'low'.
    """
    same_org = is_same_org(original_url, replacement_url)

    if source == "redirect" and same_org:
        return "high"
    if source == "redirect" and not same_org:
        return "medium"
    if source == "wayback":
        return "medium"
    if same_org:
        return "medium"
    return "low"


def generate_edit_summary(source: str, old_url: str, new_url: str) -> str:
    """Generate a natural-sounding edit summary in Spanish.

    Varies the wording using templates + dynamic domain info to avoid
    detection patterns across accounts.
    """
    from urllib.parse import urlparse
    domain = urlparse(old_url).netloc.replace("www.", "")

    if source == "redirect":
        templates = _SUMMARY_TEMPLATES_REDIRECT
    elif source == "wayback":
        templates = _SUMMARY_TEMPLATES_WAYBACK
    else:
        templates = _SUMMARY_TEMPLATES_GENERAL

    base = random.choice(templates)

    # Add domain-specific variation ~40% of the time
    if random.random() < 0.4 and domain:
        suffixes = [
            f" ({domain})",
            f" - {domain}",
            f" [{domain}]",
        ]
        base += random.choice(suffixes)

    return base


# ── browser-based validation ──────────────────────────────────────────


def check_url_status(page, url: str) -> dict:
    """Check if a URL is alive by navigating to it via Camoufox.

    Returns {"alive": bool, "final_url": str, "status": int or None}.
    """
    log.info("Checking URL status: %s", url)
    try:
        resp = page.goto(url, wait_until="load", timeout=15000)
        time.sleep(random.uniform(1.0, 2.0))

        final_url = page.url
        status = resp.status if resp else None

        alive = status is not None and 200 <= status < 400
        redirected = final_url != url

        log.info("  Status: %s, Final URL: %s, Alive: %s", status, final_url, alive)
        return {
            "alive": alive,
            "final_url": final_url,
            "status": status,
            "redirected": redirected,
        }
    except Exception as e:
        log.warning("  Failed to check %s: %s", url, e)
        return {"alive": False, "final_url": url, "status": None, "redirected": False}


def find_replacement_via_redirect(page, url: str) -> dict | None:
    """Check if the broken URL redirects to a new location.

    Returns {"replacement_url": ..., "source": "redirect"} or None.
    """
    result = check_url_status(page, url)
    if result["alive"] and result["redirected"]:
        return {
            "replacement_url": result["final_url"],
            "source": "redirect",
        }
    return None


def find_replacement_via_wayback(page, url: str) -> dict | None:
    """Check the Wayback Machine for an archived snapshot.

    Returns {"replacement_url": ..., "source": "wayback"} or None.
    """
    api_url = build_wayback_api_url(url)
    log.info("Checking Wayback Machine for: %s", url)

    try:
        page.goto(api_url, wait_until="load", timeout=8000)
        time.sleep(random.uniform(1.0, 2.0))

        body = page.query_selector("body")
        if not body:
            return None

        import json
        text = body.inner_text().strip()
        data = json.loads(text)
        result = parse_wayback_response(data)
        if result:
            return {
                "replacement_url": result["snapshot_url"],
                "source": "wayback",
            }
    except Exception as e:
        log.warning("Wayback check failed for %s: %s", url, e)

    return None


def find_replacement(page, url: str) -> dict | None:
    """Try all methods to find a replacement for a broken URL.

    Tries in order: redirect check, Wayback Machine.
    Returns the first successful result or None.
    """
    # Try redirect first (fastest, highest confidence)
    result = find_replacement_via_redirect(page, url)
    if result:
        return result

    # Try Wayback Machine
    result = find_replacement_via_wayback(page, url)
    if result:
        return result

    return None
