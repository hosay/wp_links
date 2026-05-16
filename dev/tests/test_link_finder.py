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


# ── v2 template parsing ──────────────────────────────────────────────


from dev.link_finder import extract_broken_urls_v2


def test_extract_v2_enlace_roto():
    wikitext = '* [http://broken.gob.mx/doc Informe] {{enlace roto |url=http://broken.gob.mx/doc}}'
    results = extract_broken_urls_v2(wikitext)
    assert any(r["url"] == "http://broken.gob.mx/doc" for r in results)
    assert any(r["template"] == "enlace_roto" for r in results)


def test_extract_v2_cita_web_urlmuerta():
    wikitext = '{{cita web |url=http://dead.org/page |título=Título |urlmuerta=sí}}'
    results = extract_broken_urls_v2(wikitext)
    assert any(r["url"] == "http://dead.org/page" for r in results)
    assert any(r["template"] == "cita_web" for r in results)


def test_extract_v2_cita_web_estado_muerto():
    wikitext = '{{cita web |url=https://gone.com/x |título=X |estado=muerto}}'
    results = extract_broken_urls_v2(wikitext)
    assert any(r["url"] == "https://gone.com/x" for r in results)


def test_extract_v2_url_inaccesible():
    wikitext = '[http://unavailable.net/page Sitio] {{URL inaccesible|url=http://unavailable.net/page}}'
    results = extract_broken_urls_v2(wikitext)
    assert any(r["url"] == "http://unavailable.net/page" for r in results)
    assert any(r["template"] == "url_inaccesible" for r in results)


def test_extract_v2_cite_web_dead_url():
    wikitext = '{{cite web |url=http://example.com/gone |title=Gone |dead-url=yes}}'
    results = extract_broken_urls_v2(wikitext)
    assert any(r["url"] == "http://example.com/gone" for r in results)
    assert any(r["template"] == "cite_web" for r in results)


def test_extract_v2_cite_web_url_status_dead():
    wikitext = '{{cite web |url=https://dead.io/page |title=Dead |url-status=dead}}'
    results = extract_broken_urls_v2(wikitext)
    assert any(r["url"] == "https://dead.io/page" for r in results)


def test_extract_v2_no_false_positives():
    """Living links should not be extracted."""
    wikitext = '{{cita web |url=http://alive.com/page |título=Alive}}'
    results = extract_broken_urls_v2(wikitext)
    assert len(results) == 0


def test_extract_v2_deduplicates():
    """Same URL appearing in multiple templates should appear once."""
    wikitext = """
    [http://x.com/a Link] {{enlace roto |url=http://x.com/a}}
    {{cita web |url=http://x.com/a |título=X |urlmuerta=sí}}
    """
    results = extract_broken_urls_v2(wikitext)
    urls = [r["url"] for r in results]
    assert urls.count("http://x.com/a") == 1


# ── API-based discovery ──────────────────────────────────────────────


from unittest.mock import patch, MagicMock
from dev.link_finder import fetch_category_members_api, fetch_wikitext_batch_api


def test_fetch_category_members_api_parses_response():
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "query": {
            "categorymembers": [
                {"title": "Artículo_1", "ns": 0},
                {"title": "Artículo_2", "ns": 0},
            ]
        }
    }
    with patch("dev.link_finder.http_requests.get", return_value=mock_response):
        titles, cont = fetch_category_members_api()
    assert titles == ["Artículo_1", "Artículo_2"]
    assert cont is None


def test_fetch_category_members_api_handles_continue():
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "query": {"categorymembers": [{"title": "Art1", "ns": 0}]},
        "continue": {"cmcontinue": "page2token"},
    }
    with patch("dev.link_finder.http_requests.get", return_value=mock_response):
        titles, cont = fetch_category_members_api()
    assert titles == ["Art1"]
    assert cont == "page2token"


def test_fetch_wikitext_batch_api():
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "query": {
            "pages": {
                "123": {
                    "title": "Ejemplo",
                    "revisions": [{"*": "== Sección ==\nTexto del artículo"}],
                },
                "456": {
                    "title": "Prueba",
                    "revisions": [{"*": "Contenido de prueba"}],
                },
            }
        }
    }
    with patch("dev.link_finder.http_requests.get", return_value=mock_response):
        result = fetch_wikitext_batch_api(["Ejemplo", "Prueba"])
    assert result["Ejemplo"] == "== Sección ==\nTexto del artículo"
    assert result["Prueba"] == "Contenido de prueba"
