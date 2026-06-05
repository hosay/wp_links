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


def test_apply_link_fix_preserves_other_enlace_roto():
    """Only remove {{enlace roto}} associated with the fixed URL, not others."""
    wikitext = (
        '* [http://old.gob.mx/doc informe] {{enlace roto |url=http://old.gob.mx/doc}}\n'
        '* [http://other.com/page texto] {{enlace roto |url=http://other.com/page}}\n'
    )
    fixed = apply_link_fix(wikitext, "http://old.gob.mx/doc", "http://new.gob.mx/doc")
    assert "http://new.gob.mx/doc" in fixed
    # The OTHER broken link template must survive
    assert "enlace roto" in fixed
    assert "http://other.com/page" in fixed


def test_apply_link_fix_preserves_dead_params_on_other_refs():
    """Only strip |urlmuerta=sí from the citation containing the fixed URL."""
    wikitext = (
        '{{Cita web |url=http://old.gob.mx/doc |título=Doc |urlmuerta=sí}}\n'
        '{{Cita web |url=http://other.com/page |título=Other |urlmuerta=sí}}\n'
    )
    fixed = apply_link_fix(wikitext, "http://old.gob.mx/doc", "http://new.gob.mx/doc")
    assert "http://new.gob.mx/doc" in fixed
    # The fixed citation should NOT have urlmuerta
    lines = fixed.strip().split('\n')
    assert 'urlmuerta' not in lines[0]
    # The other citation MUST keep urlmuerta
    assert 'urlmuerta' in lines[1]


def test_apply_link_fix_multiline_citation():
    """Strip dead-link params from multi-line citation templates."""
    wikitext = (
        '{{cita web\n'
        ' |url=http://old.gob.mx/doc\n'
        ' |título=Informe oficial\n'
        ' |urlmuerta=sí\n'
        ' |fechaacceso=2020-01-01\n'
        '}}'
    )
    fixed = apply_link_fix(wikitext, "http://old.gob.mx/doc", "http://new.gob.mx/doc")
    assert "http://new.gob.mx/doc" in fixed
    assert "urlmuerta" not in fixed
    assert "título=Informe oficial" in fixed
    assert "fechaacceso" in fixed


def test_apply_link_fix_multiline_estado_muerto():
    """Strip |estado=muerto from multi-line cite web."""
    wikitext = (
        '{{cite web\n'
        ' |url=http://old.example.com/page\n'
        ' |title=Some Page\n'
        ' |url-status=dead\n'
        '}}'
    )
    fixed = apply_link_fix(wikitext, "http://old.example.com/page", "http://new.example.com/page")
    assert "http://new.example.com/page" in fixed
    assert "url-status" not in fixed
    assert "title=Some Page" in fixed


def test_apply_link_fix_multiline_preserves_other_citation():
    """Multi-line fix must not touch a different citation's dead params."""
    wikitext = (
        '{{cita web\n'
        ' |url=http://old.gob.mx/doc\n'
        ' |título=Doc\n'
        ' |urlmuerta=sí\n'
        '}}\n'
        '{{cita web\n'
        ' |url=http://other.com/page\n'
        ' |título=Other\n'
        ' |urlmuerta=sí\n'
        '}}'
    )
    fixed = apply_link_fix(wikitext, "http://old.gob.mx/doc", "http://new.gob.mx/doc")
    assert "http://new.gob.mx/doc" in fixed
    # The other citation must keep its urlmuerta
    other_start = fixed.find("http://other.com/page")
    assert other_start != -1
    remaining = fixed[other_start:]
    assert "urlmuerta" in remaining


def test_apply_link_fix_enlace_roto_different_line():
    """Remove {{enlace roto |url=old_url}} even when on a different line."""
    wikitext = (
        '<ref>[http://old.gob.mx/doc Informe]</ref>\n'
        '{{enlace roto |url=http://old.gob.mx/doc}}'
    )
    fixed = apply_link_fix(wikitext, "http://old.gob.mx/doc", "http://new.gob.mx/doc")
    assert "http://new.gob.mx/doc" in fixed
    assert "enlace roto" not in fixed


def test_apply_link_fix_citation_params_after_enlace_roto_removal():
    """Dead-link params in citation must be stripped even when enlace_roto also references the URL."""
    wikitext = (
        '{{cita web |url=http://old.gob.mx/doc |título=Informe |urlmuerta=sí}}\n'
        '{{enlace roto |url=http://old.gob.mx/doc}}'
    )
    fixed = apply_link_fix(wikitext, "http://old.gob.mx/doc", "http://new.gob.mx/doc")
    assert "http://new.gob.mx/doc" in fixed
    assert "enlace roto" not in fixed
    assert "urlmuerta" not in fixed


def test_apply_link_fix_enlace_roto_sole_ref_content():
    """When {{enlace roto}} is the entire <ref> content, convert to a bare link."""
    wikitext = (
        '<ref>{{enlace roto|1=http://old.gob.mx/doc |2=http://old.gob.mx/doc '
        '|bot=InternetArchiveBot }}</ref>'
    )
    fixed = apply_link_fix(wikitext, "http://old.gob.mx/doc", "http://new.gob.mx/doc")
    assert "http://new.gob.mx/doc" in fixed
    assert "enlace roto" not in fixed
    # Must NOT leave an empty <ref></ref>
    assert "<ref></ref>" not in fixed
    # Should have a valid ref with the new URL
    assert "<ref>" in fixed


def test_apply_link_fix_enlace_roto_sole_ref_url_param():
    """{{enlace roto |url=X}} as sole ref content → replace with new URL."""
    wikitext = '<ref>{{enlace roto |url=http://dead.example.com/page}}</ref>'
    fixed = apply_link_fix(wikitext, "http://dead.example.com/page", "http://alive.example.com/page")
    assert "http://alive.example.com/page" in fixed
    assert "enlace roto" not in fixed
    assert "<ref></ref>" not in fixed


def test_apply_link_fix_same_line_unrelated_enlace_roto_preserved():
    """Fixing URL-A must not strip an {{enlace roto}} template for URL-B on the same line."""
    wikitext = (
        '<ref>{{Cita web |url=http://example.com/a |título=A}}</ref>'
        '<ref>{{enlace roto|1=http://example.com/b |2=http://example.com/b '
        '|bot=InternetArchiveBot }}</ref>'
        '<ref>http://example.com/c</ref>'
    )
    # Fix URL-C (bare URL) — should NOT touch the enlace roto for URL-B
    fixed = apply_link_fix(wikitext, "http://example.com/c", "http://example.com/c-new")
    assert "http://example.com/c-new" in fixed
    # The enlace roto template for URL-B must still be there
    assert "enlace roto" in fixed
    assert "example.com/b" in fixed
    # No empty refs
    assert "<ref></ref>" not in fixed


def test_pick_typo_edit_summary():
    s = pick_typo_edit_summary()
    assert isinstance(s, str)
    assert s in TYPO_SUMMARIES
