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


def _find_enclosing_template(text: str, pos: int) -> tuple[int, int] | None:
    """Find the {{...}} template boundaries enclosing position `pos`.

    Handles nested templates by tracking brace depth.
    Returns (start, end) indices or None if not inside a template.
    """
    # Walk backwards to find opening {{
    depth = 0
    i = pos
    start = None
    while i >= 1:
        if text[i - 1:i + 1] == '}}':
            depth += 1
            i -= 2
        elif text[i - 1:i + 1] == '{{':
            if depth == 0:
                start = i - 1
                break
            depth -= 1
            i -= 2
        else:
            i -= 1

    if start is None:
        return None

    # Walk forwards from start to find matching }}
    depth = 1
    i = start + 2
    while i < len(text) - 1:
        if text[i:i + 2] == '{{':
            depth += 1
            i += 2
        elif text[i:i + 2] == '}}':
            depth -= 1
            if depth == 0:
                return (start, i + 2)
            i += 2
        else:
            i += 1

    return None


def apply_link_fix(wikitext: str, old_url: str, new_url: str) -> str:
    """Replace a broken URL and remove associated dead-link markers.

    Scoped removal: only strips broken-link templates and dead-URL
    parameters that belong to the specific URL being fixed. Other
    broken links in the article are left untouched.

    Handles single-line and multi-line citation templates.
    """
    enlace_roto_re = re.compile(
        r'\s*\{\{enlace roto(?:\s*\|[^}]*)?\}\}',
        re.IGNORECASE,
    )
    url_inaccesible_re = re.compile(
        r'\s*\{\{URL inaccesible(?:\s*\|[^}]*)?\}\}',
        re.IGNORECASE,
    )
    dead_param_patterns = [
        re.compile(r'\s*\|urlmuerta\s*=\s*s[ií]\s*', re.IGNORECASE),
        re.compile(r'\s*\|estado\s*=\s*muerto\s*', re.IGNORECASE),
        re.compile(r'\s*\|dead-url\s*=\s*yes\s*', re.IGNORECASE),
        re.compile(r'\s*\|url-status\s*=\s*dead\s*', re.IGNORECASE),
    ]

    result = wikitext

    # Step 1: Handle {{enlace roto}} / {{URL inaccesible}} on same line as old_url.
    # If old_url also exists outside the template (e.g. in a [url Title] link),
    # just remove the template. If old_url is ONLY inside the template (the
    # template IS the reference), replace it with new_url to avoid leaving
    # empty <ref></ref> tags.
    lines = result.split('\n')
    cleaned_lines = []
    for line in lines:
        if old_url in line:
            stripped = enlace_roto_re.sub('', line)
            stripped = url_inaccesible_re.sub('', stripped)
            if old_url in stripped:
                # URL survives outside the template — safe to just remove
                line = stripped
            else:
                # URL only inside template — replace template with new URL
                line = enlace_roto_re.sub(f' {new_url}', line)
                line = url_inaccesible_re.sub(f' {new_url}', line)
        cleaned_lines.append(line)
    result = '\n'.join(cleaned_lines)

    # Step 2: Strip dead-link params from the enclosing citation template
    # (must run before enlace_roto removal so old_url is still findable)
    pos = result.find(old_url)
    if pos != -1:
        tmpl = _find_enclosing_template(result, pos)
        if tmpl:
            start, end = tmpl
            block = result[start:end]
            if re.match(r'\{\{(?:cita web|cite web)\b', block, re.IGNORECASE):
                for pat in dead_param_patterns:
                    block = pat.sub('', block)
                result = result[:start] + block + result[end:]

    # Step 3: Remove {{enlace roto |url=<old_url>}} even on a different line.
    # Same logic: replace with new_url if it's the sole reference content.
    specific_enlace_re = re.compile(
        r'\s*\{\{enlace roto\s*\|[^}]*?' + re.escape(old_url) + r'[^}]*?\}\}',
        re.IGNORECASE | re.DOTALL,
    )
    if specific_enlace_re.search(result):
        test_result = specific_enlace_re.sub('', result)
        if old_url in test_result:
            result = test_result
        else:
            result = specific_enlace_re.sub(f' {new_url}', result)

    # Step 4: Replace the URL
    result = result.replace(old_url, new_url)

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
