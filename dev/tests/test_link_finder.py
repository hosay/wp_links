"""Tests for dev.link_finder — broken link discovery."""

import pytest

from dev.link_finder import (
    parse_dead_links_report,
    extract_broken_urls_from_wikitext,
)


SAMPLE_DEAD_LINKS_HTML = """
<ul>
<li><a href="/wiki/Ejemplo" title="Ejemplo">Ejemplo</a> ‎
  <span class="mw-changeslist-links">
    <a rel="nofollow" class="external text" href="http://broken.gob.mx/doc">http://broken.gob.mx/doc</a>
  </span>
</li>
<li><a href="/wiki/Prueba" title="Prueba">Prueba</a> ‎
  <span class="mw-changeslist-links">
    <a rel="nofollow" class="external text" href="http://dead.org/page">http://dead.org/page</a>
  </span>
</li>
</ul>
"""


def test_parse_dead_links_report():
    results = parse_dead_links_report(SAMPLE_DEAD_LINKS_HTML)
    assert len(results) == 2
    assert results[0]["wiki_title"] == "Ejemplo"
    assert results[0]["broken_url"] == "http://broken.gob.mx/doc"
    assert results[1]["wiki_title"] == "Prueba"


def test_parse_dead_links_report_empty():
    assert parse_dead_links_report("<div>No dead links</div>") == []


def test_extract_broken_urls_from_wikitext():
    """Extract URLs that have {{enlace roto}} templates next to them."""
    wikitext = """
    * [http://broken.gob.mx/doc Informe] {{enlace roto |url=http://broken.gob.mx/doc}}
    * [https://alive.com/page Alive page]
    * {{Cita web |url=http://dead.org/x |título=Dead}} {{enlace roto}}
    """
    broken = extract_broken_urls_from_wikitext(wikitext)
    assert "http://broken.gob.mx/doc" in broken
    # alive.com should not be in broken list since it has no enlace roto marker
    assert "https://alive.com/page" not in broken


def test_extract_broken_urls_empty():
    assert extract_broken_urls_from_wikitext("No hay enlaces rotos") == []
