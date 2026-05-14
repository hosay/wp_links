"""Tests for dev.wiki_browser — Wikipedia interaction via Camoufox."""

import re
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from dev.wiki_browser import (
    extract_external_urls,
    build_edit_url,
    build_raw_url,
    replace_url_in_wikitext,
    HUMAN_DELAY_MIN,
    HUMAN_DELAY_MAX,
)


# ── pure functions (no browser needed) ────────────────────────────────


def test_extract_external_urls():
    wikitext = """
    Según el [http://www.old-gov.mx/doc informe oficial] y la
    [https://example.org/page página de ejemplo], el tema fue tratado.
    También ver {{Cita web |url=http://broken.gob.mx/page |título=Ref}}.
    """
    urls = extract_external_urls(wikitext)
    assert "http://www.old-gov.mx/doc" in urls
    assert "https://example.org/page" in urls
    assert "http://broken.gob.mx/page" in urls


def test_extract_external_urls_empty():
    assert extract_external_urls("No hay enlaces aquí.") == []


def test_extract_external_urls_deduplicates():
    wikitext = "[http://a.com x] y [http://a.com z]"
    urls = extract_external_urls(wikitext)
    assert urls.count("http://a.com") == 1


def test_build_edit_url():
    url = build_edit_url("Ejemplo")
    assert "es.wikipedia.org" in url
    assert "action=edit" in url
    assert "Ejemplo" in url


def test_build_raw_url():
    url = build_raw_url("Ejemplo")
    assert "es.wikipedia.org" in url
    assert "action=raw" in url


def test_replace_url_in_wikitext():
    wikitext = "Ver [http://old.gob.mx/doc informe] para más info."
    new = replace_url_in_wikitext(wikitext, "http://old.gob.mx/doc", "http://new.gob.mx/doc")
    assert "http://new.gob.mx/doc" in new
    assert "http://old.gob.mx/doc" not in new


def test_replace_url_in_wikitext_template():
    wikitext = '{{Cita web |url=http://old.gob.mx/doc |título=Ref}}'
    new = replace_url_in_wikitext(wikitext, "http://old.gob.mx/doc", "http://new.gob.mx/doc")
    assert "http://new.gob.mx/doc" in new


def test_replace_url_preserves_surrounding_text():
    wikitext = "Antes http://old.com/x después"
    new = replace_url_in_wikitext(wikitext, "http://old.com/x", "http://new.com/x")
    assert new == "Antes http://new.com/x después"


def test_human_delay_bounds():
    assert HUMAN_DELAY_MIN >= 2.0
    assert HUMAN_DELAY_MAX <= 10.0
    assert HUMAN_DELAY_MIN < HUMAN_DELAY_MAX
