"""Tests for dev.edit_engine — typo and link fix edit logic."""

import json

import pytest

from dev.edit_engine import (
    load_typo_patterns,
    find_typo_in_text,
    apply_typo_fix,
    apply_link_fix,
    pick_typo_edit_summary,
    TYPO_SUMMARIES,
)


@pytest.fixture
def typo_patterns():
    return load_typo_patterns()


def test_load_typo_patterns():
    patterns = load_typo_patterns()
    assert len(patterns) >= 10
    assert all("wrong" in p and "correct" in p for p in patterns)


def test_find_typo_in_text(typo_patterns):
    # Embed a known typo in wikitext
    text = "Este articulo trata sobre la historia de México."
    match = find_typo_in_text(text, typo_patterns)
    assert match is not None
    assert match["wrong"] == "articulo"
    assert match["correct"] == "artículo"


def test_find_typo_case_insensitive(typo_patterns):
    text = "El Articulo principal fue escrito en 2020."
    match = find_typo_in_text(text, typo_patterns)
    assert match is not None


def test_find_typo_no_match(typo_patterns):
    text = "Este artículo ya está correctamente escrito."
    match = find_typo_in_text(text, typo_patterns)
    assert match is None


def test_find_typo_word_boundary(typo_patterns):
    """Should not match partial words like 'articuloS' as a standalone typo."""
    text = "Los artículos están bien."  # correct word, no match
    match = find_typo_in_text(text, typo_patterns)
    assert match is None


def test_apply_typo_fix():
    text = "Este articulo es interesante y el articulo de abajo también."
    fixed, count = apply_typo_fix(text, "articulo", "artículo")
    assert "artículo" in fixed
    assert count >= 1


def test_apply_typo_fix_preserves_case():
    text = "El Articulo principal menciona el articulo secundario."
    fixed, count = apply_typo_fix(text, "articulo", "artículo")
    assert "Artículo" in fixed  # Capital preserved
    assert "artículo" in fixed  # Lowercase fixed


def test_apply_link_fix():
    wikitext = "Ver [http://old.gob.mx/doc informe] para detalles."
    fixed = apply_link_fix(wikitext, "http://old.gob.mx/doc", "http://new.gob.mx/doc")
    assert "http://new.gob.mx/doc" in fixed
    assert "http://old.gob.mx/doc" not in fixed


def test_apply_link_fix_removes_enlace_roto():
    wikitext = '[http://old.gob.mx/doc informe] {{enlace roto |url=http://old.gob.mx/doc}}'
    fixed = apply_link_fix(wikitext, "http://old.gob.mx/doc", "http://new.gob.mx/doc")
    assert "http://new.gob.mx/doc" in fixed
    assert "enlace roto" not in fixed


def test_pick_typo_edit_summary():
    s = pick_typo_edit_summary()
    assert isinstance(s, str)
    assert s in TYPO_SUMMARIES
