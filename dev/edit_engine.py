"""Edit engine — typo fixes (warmup) and link fixes.

Operates on wikitext strings. Browser interaction is handled by
wiki_browser.py; this module is pure logic + wikitext manipulation.
"""

import json
import logging
import os
import random
import re

log = logging.getLogger(__name__)

TYPO_PATTERNS_PATH = os.path.join(os.path.dirname(__file__), "data", "typo_patterns.json")

TYPO_SUMMARIES = [
    "Corrección ortográfica",
    "Corrección de acentos",
    "Ortografía",
    "Corrección tipográfica menor",
    "Arreglo de tildes",
    "Corrección de acento faltante",
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
    """Replace a broken URL and remove any {{enlace roto}} template for it."""
    # Replace the URL
    result = wikitext.replace(old_url, new_url)

    # Remove {{enlace roto}} templates that reference this URL
    # Pattern: {{enlace roto |url=... |...}} or just {{enlace roto}}
    enlace_roto_re = re.compile(
        r'\s*\{\{enlace roto(?:\s*\|[^}]*)?\}\}',
        re.IGNORECASE,
    )
    result = enlace_roto_re.sub('', result)

    return result


def pick_typo_edit_summary() -> str:
    """Pick a random typo-fix edit summary."""
    return random.choice(TYPO_SUMMARIES)
