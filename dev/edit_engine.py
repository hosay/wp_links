"""Edit engine — typo fixes (warmup) and link fixes.

Operates on wikitext strings. Browser interaction is handled by
wiki_browser.py; this module is pure logic + wikitext manipulation.
"""

import json
import logging
import os
import random
import re

import requests

log = logging.getLogger(__name__)

TYPO_PATTERNS_PATH = os.path.join(os.path.dirname(__file__), "data", "typo_patterns.json")

TYPO_SUMMARIES = [
    "Corrección ortográfica",
    "Corrección de acentos",
    "Ortografía",
    "Corrección tipográfica menor",
    "Arreglo de tildes",
    "Corrección de acento faltante",
    "Tilde faltante",
    "Corrección de tilde",
    "Mejora ortográfica",
    "Acento omitido",
    "Ortografía: acento faltante",
    "Fijación de acentuación",
    "Corrección menor de ortografía",
    "Revisión ortográfica",
    "Acento diacrítico",
    "Corrección de escritura",
    "Normalización ortográfica",
    "Tilde omitida",
    "Error de acento corregido",
    "Ajuste ortográfico",
]


def load_typo_patterns() -> list[dict]:
    """Load typo patterns from JSON data file."""
    with open(TYPO_PATTERNS_PATH) as f:
        return json.load(f)


def find_typo_in_text(text: str, patterns: list[dict]) -> dict | None:
    """Find the first matching typo pattern in text.

    Uses word boundary matching to avoid partial matches.
    Returns the matching pattern dict or None.
    """
    for pattern in patterns:
        wrong = pattern["wrong"]
        # Word boundary match, case-insensitive
        regex = re.compile(rf'\b{re.escape(wrong)}\b', re.IGNORECASE)
        if regex.search(text):
            return pattern
    return None


def apply_typo_fix(text: str, wrong: str, correct: str) -> tuple[str, int]:
    """Replace all occurrences of a typo in text, preserving case.

    Returns (fixed_text, replacement_count).
    """
    count = 0

    def _replace(match):
        nonlocal count
        count += 1
        original = match.group(0)
        # Preserve case pattern
        if original[0].isupper():
            return correct[0].upper() + correct[1:]
        return correct

    regex = re.compile(rf'\b{re.escape(wrong)}\b', re.IGNORECASE)
    fixed = regex.sub(_replace, text)
    return fixed, count


def apply_link_fix(wikitext: str, old_url: str, new_url: str) -> str:
    """Replace a broken URL and remove dead-link markers.

    Handles:
    - {{enlace roto|...}}
    - {{URL inaccesible|...}}
    - |urlmuerta=sí in {{cita web}} templates
    - |estado=muerto in {{cita web}} templates
    - |dead-url=yes and |url-status=dead in {{cite web}} templates
    """
    # Replace the URL
    result = wikitext.replace(old_url, new_url)

    # Remove {{enlace roto}} templates
    enlace_roto_re = re.compile(
        r'\s*\{\{enlace roto(?:\s*\|[^}]*)?\}\}',
        re.IGNORECASE,
    )
    result = enlace_roto_re.sub('', result)

    # Remove {{URL inaccesible}} templates
    url_inaccesible_re = re.compile(
        r'\s*\{\{URL inaccesible(?:\s*\|[^}]*)?\}\}',
        re.IGNORECASE,
    )
    result = url_inaccesible_re.sub('', result)

    # Remove dead-link parameters from citation templates
    # |urlmuerta=sí or |urlmuerta=si
    result = re.sub(r'\s*\|urlmuerta\s*=\s*s[ií]\s*', ' ', result, flags=re.IGNORECASE)
    # |estado=muerto
    result = re.sub(r'\s*\|estado\s*=\s*muerto\s*', ' ', result, flags=re.IGNORECASE)
    # |dead-url=yes
    result = re.sub(r'\s*\|dead-url\s*=\s*yes\s*', ' ', result, flags=re.IGNORECASE)
    # |url-status=dead
    result = re.sub(r'\s*\|url-status\s*=\s*dead\s*', ' ', result, flags=re.IGNORECASE)

    return result


def pick_typo_edit_summary() -> str:
    """Pick a random typo-fix edit summary."""
    return random.choice(TYPO_SUMMARIES)


SPACING_SUMMARIES = [
    "Espaciado",
    "Corrección de espacios",
    "Espacio doble eliminado",
    "Formato: espacios extra",
    "Corrección tipográfica",
    "Espacios redundantes",
    "Limpieza de espacios",
    "Corrección de formato menor",
    "Eliminar espacios dobles",
    "Ajuste de espaciado",
]

_DOUBLE_SPACE_RE = re.compile(r'(?<!\n) {2,}(?!\n)')


def find_double_spaces(text: str) -> bool:
    """Return True if the text contains double spaces (outside of newlines)."""
    return bool(_DOUBLE_SPACE_RE.search(text))


def apply_spacing_fix(text: str) -> tuple[str, int]:
    """Collapse all double+ spaces (not at line boundaries) to single spaces.

    Returns (fixed_text, replacement_count).
    """
    count = [0]

    def _replace(m):
        count[0] += 1
        return ' '

    fixed = _DOUBLE_SPACE_RE.sub(_replace, text)
    return fixed, count[0]


def pick_spacing_edit_summary() -> str:
    """Pick a random spacing-fix edit summary."""
    return random.choice(SPACING_SUMMARIES)


def search_articles_with_typo(wrong: str, limit: int = 20) -> list[str]:
    """Use MediaWiki API insource search to find articles containing a typo.

    Returns a list of article titles. Much more efficient than random browsing.
    """
    api_url = "https://es.wikipedia.org/w/api.php"
    params = {
        "action": "query",
        "list": "search",
        "srsearch": f'insource:"{wrong}"',
        "srnamespace": 0,
        "srlimit": limit,
        "format": "json",
    }
    try:
        resp = requests.get(api_url, params=params, timeout=15,
                            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0"})
        resp.raise_for_status()
        data = resp.json()
        results = data.get("query", {}).get("search", [])
        titles = [r["title"] for r in results]
        log.info("API search for '%s' found %d candidate articles", wrong, len(titles))
        return titles
    except Exception as exc:
        log.warning("API search failed for '%s': %s", wrong, exc)
        return []
