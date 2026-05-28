"""Tests for replacement URL validation gate (Tier 1: liveness, Tier 2: Gemini content check)."""

from unittest.mock import patch, MagicMock

import pytest

from dev.link_replacer import verify_replacement_live, verify_replacement_content


# ── Tier 1: HTTP liveness check ──────────────────────────────────────


def test_verify_live_accepts_200_with_content():
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = "<html><head><title>Real Article</title></head><body>" + "x" * 2000 + "</body></html>"
    mock_resp.url = "http://example.com/article"

    with patch("dev.link_replacer.requests.get", return_value=mock_resp):
        result = verify_replacement_live("http://example.com/article")
    assert result["alive"] is True
    assert result["soft_404"] is False


def test_verify_live_rejects_hard_404():
    mock_resp = MagicMock()
    mock_resp.status_code = 404
    mock_resp.text = "<html><title>Not Found</title><body>404</body></html>"

    with patch("dev.link_replacer.requests.get", return_value=mock_resp):
        result = verify_replacement_live("http://example.com/gone")
    assert result["alive"] is False


def test_verify_live_detects_soft_404_in_title():
    """Page returns 200 but title says 'Page not found'."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = "<html><head><title>Page not found - Example</title></head><body>" + "x" * 2000 + "</body></html>"
    mock_resp.url = "http://example.com/gone"

    with patch("dev.link_replacer.requests.get", return_value=mock_resp):
        result = verify_replacement_live("http://example.com/gone")
    assert result["alive"] is False
    assert result["soft_404"] is True


def test_verify_live_detects_soft_404_spanish():
    """Detects Spanish soft 404 pages."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = "<html><head><title>Página no encontrada</title></head><body>" + "x" * 2000 + "</body></html>"
    mock_resp.url = "http://example.com/gone"

    with patch("dev.link_replacer.requests.get", return_value=mock_resp):
        result = verify_replacement_live("http://example.com/gone")
    assert result["alive"] is False
    assert result["soft_404"] is True


def test_verify_live_rejects_tiny_pages():
    """Pages under 1KB are likely error pages."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = "<html><title>OK</title><body>tiny</body></html>"
    mock_resp.url = "http://example.com/page"

    with patch("dev.link_replacer.requests.get", return_value=mock_resp):
        result = verify_replacement_live("http://example.com/page")
    assert result["alive"] is False


def test_verify_live_handles_timeout():
    with patch("dev.link_replacer.requests.get", side_effect=Exception("Timeout")):
        result = verify_replacement_live("http://slow.com/page")
    assert result["alive"] is False


def test_verify_live_detects_error_page_in_body():
    """Page returns 200 but body contains 'ha ocurrido un error'."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = (
        "<html><head><title>24horas</title></head>"
        "<body><h1>Ha ocurrido un error</h1>" + "x" * 2000 + "</body></html>"
    )
    mock_resp.url = "http://24horas.cl/page"

    with patch("dev.link_replacer.requests.get", return_value=mock_resp):
        result = verify_replacement_live("http://24horas.cl/page")
    assert result["alive"] is False
    assert result["soft_404"] is True


# ── Tier 2: Gemini content relevance check ───────────────────────────


def _patch_gemini_key():
    return patch("dev.link_replacer.GEMINI_API_KEY", "fake-key")


def test_verify_content_accepts_relevant_page():
    """Gemini confirms replacement content is relevant to article context."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "candidates": [{"content": {"parts": [{"text": '{"is_relevant": true, "reasoning": "Content matches"}'}]}}],
        "usageMetadata": {"promptTokenCount": 100, "candidatesTokenCount": 20},
    }
    with _patch_gemini_key(), patch("dev.link_replacer.requests.post", return_value=mock_resp):
        result = verify_replacement_content(
            replacement_url="http://new.com/article",
            replacement_text="Article about climate change and deforestation",
            article_title="Deforestación en la Amazonia",
            original_url="http://old.com/article",
        )
    assert result["is_relevant"] is True


def test_verify_content_rejects_generic_index():
    """Gemini detects replacement is a generic index/listing page, not specific content."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "candidates": [{"content": {"parts": [{"text": '{"is_relevant": false, "reasoning": "Generic news index page, not the specific article"}'}]}}],
        "usageMetadata": {"promptTokenCount": 100, "candidatesTokenCount": 20},
    }
    with _patch_gemini_key(), patch("dev.link_replacer.requests.post", return_value=mock_resp):
        result = verify_replacement_content(
            replacement_url="http://abc.es/ultimas-noticias/",
            replacement_text="Noticias de última hora ABC España noticias del mundo...",
            article_title="1. Bundesliga 2012-13",
            original_url="http://abc.es/agencias/noticia.asp?noticia=131742",
        )
    assert result["is_relevant"] is False


def test_verify_content_handles_api_failure():
    """On API failure, defaults to relevant (Tier 1 already passed)."""
    with _patch_gemini_key(), patch("dev.link_replacer.requests.post", side_effect=Exception("API down")):
        result = verify_replacement_content(
            replacement_url="http://new.com/page",
            replacement_text="Some content",
            article_title="Some Article",
            original_url="http://old.com/page",
        )
    # Tier 1 already validated liveness — don't block on transient API errors
    assert result["is_relevant"] is True


def test_verify_content_skips_when_no_api_key():
    """Without Gemini key, defaults to relevant (skip check)."""
    with patch("dev.link_replacer.GEMINI_API_KEY", ""):
        result = verify_replacement_content(
            replacement_url="http://new.com/page",
            replacement_text="Some content",
            article_title="Some Article",
            original_url="http://old.com/page",
        )
    # No API key = can't check = assume relevant (Tier 1 already passed)
    assert result["is_relevant"] is True


# ── Integration: discovery pipeline with validation gate ─────────────


def test_discovery_rejects_replacement_that_fails_liveness(db_fixture):
    """Discovery pipeline should NOT store a replacement if liveness check fails."""
    from dev.db import get_fixable_links, get_broken_links_needing_replacement
    from dev.discovery import discover_broken_links

    mock_category = (["TestArticle"], None)
    mock_wikitext = {"TestArticle": '{{enlace roto |url=http://dead.com/page}}'}
    mock_replacement = {
        "replacement_url": "http://also-dead.com/page",
        "source": "redirect",
        "similarity_score": 1.0,
        "wayback_snapshot_url": None,
        "search_query": None,
    }

    with patch("dev.discovery.fetch_category_members_api", return_value=mock_category):
        with patch("dev.discovery.fetch_wikitext_batch_api", return_value=mock_wikitext):
            with patch("dev.discovery.find_live_replacement", return_value=mock_replacement):
                with patch("dev.discovery.verify_replacement_live") as mock_verify:
                    mock_verify.return_value = {"alive": False, "soft_404": False}
                    stats = discover_broken_links(db_fixture, max_articles=5)

    assert stats["replacements_found"] == 0
    assert get_fixable_links(db_fixture) == []
    # Link should be marked as searched so it doesn't re-enter the queue
    needing = get_broken_links_needing_replacement(db_fixture)
    assert len(needing) == 0


@pytest.fixture
def db_fixture():
    from dev.db import init_db
    conn = init_db(":memory:")
    yield conn
    conn.close()
