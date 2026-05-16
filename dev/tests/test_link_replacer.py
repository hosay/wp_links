"""Tests for dev.link_replacer — smart replacement URL discovery engine."""

import pytest
from unittest.mock import patch, MagicMock

from dev.link_replacer import (
    fetch_wayback_content,
    extract_distinctive_phrase,
    google_search,
    fetch_candidate_page,
    compute_similarity,
    find_live_replacement,
)


# ── fetch_wayback_content ────────────────────────────────────────────


def test_fetch_wayback_content_success():
    """Fetches archived page and returns text + title."""
    # Mock the availability check
    avail_resp = MagicMock()
    avail_resp.status_code = 200
    avail_resp.json.return_value = {
        "archived_snapshots": {
            "closest": {
                "available": True,
                "url": "http://web.archive.org/web/20230101/http://example.com/page",
                "timestamp": "20230101000000",
                "status": "200",
            }
        }
    }

    # Mock the snapshot page fetch
    page_resp = MagicMock()
    page_resp.status_code = 200
    page_resp.text = """
    <html><head><title>Example Article Title</title></head>
    <body><p>This is a detailed article about renewable energy sources in Latin America.</p></body>
    </html>
    """

    with patch("dev.link_replacer.requests.get", side_effect=[avail_resp, page_resp]):
        result = fetch_wayback_content("http://example.com/page")

    assert result is not None
    assert result["title"] == "Example Article Title"
    assert "renewable energy" in result["text"]
    assert "web.archive.org" in result["snapshot_url"]


def test_fetch_wayback_content_not_found():
    """Returns None when no snapshot available."""
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"archived_snapshots": {}}

    with patch("dev.link_replacer.requests.get", return_value=resp):
        result = fetch_wayback_content("http://gone-forever.com")
    assert result is None


# ── extract_distinctive_phrase ───────────────────────────────────────


def test_extract_distinctive_phrase_picks_body_content():
    text = (
        "Navigation Home About Contact "
        "This is a detailed article discussing the impact of climate change "
        "on agricultural production in the Central Valley region of Chile."
    )
    phrase = extract_distinctive_phrase(text)
    assert phrase is not None
    words = phrase.split()
    assert 6 <= len(words) <= 12


def test_extract_distinctive_phrase_short_text_returns_none():
    assert extract_distinctive_phrase("Too short") is None


def test_extract_distinctive_phrase_skips_generic():
    text = "Home About Contact Privacy Policy Terms of Service"
    result = extract_distinctive_phrase(text)
    # Should return None for purely navigational text
    assert result is None


# ── google_search ────────────────────────────────────────────────────


def _patch_tavily_key():
    """Context manager to patch Tavily API key for testing."""
    return patch("dev.link_replacer.TAVILY_KEY", "fake-tavily-key")


def test_google_search_parses_results():
    mock_client = MagicMock()
    mock_client.search.return_value = {
        "results": [
            {"title": "Result 1", "url": "http://site1.com/page", "content": "Snippet 1"},
            {"title": "Result 2", "url": "http://site2.com/page", "content": "Snippet 2"},
        ]
    }
    with _patch_tavily_key(), patch("dev.link_replacer.TavilyClient", return_value=mock_client):
        results = google_search("test query")
    assert len(results) == 2
    assert results[0]["link"] == "http://site1.com/page"


def test_google_search_handles_no_results():
    mock_client = MagicMock()
    mock_client.search.return_value = {"results": []}
    with _patch_tavily_key(), patch("dev.link_replacer.TavilyClient", return_value=mock_client):
        results = google_search("obscure query no results")
    assert results == []


def test_google_search_handles_api_error():
    mock_client = MagicMock()
    mock_client.search.side_effect = Exception("API error")
    with _patch_tavily_key(), patch("dev.link_replacer.TavilyClient", return_value=mock_client):
        results = google_search("any query")
    assert results == []


# ── fetch_candidate_page ─────────────────────────────────────────────


def test_fetch_candidate_page_success():
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = "<html><head><title>New Page</title></head><body><p>Content here</p></body></html>"
    mock_resp.url = "http://newsite.com/page"

    with patch("dev.link_replacer.requests.get", return_value=mock_resp):
        result = fetch_candidate_page("http://newsite.com/page")
    assert result is not None
    assert result["title"] == "New Page"
    assert result["status"] == 200


def test_fetch_candidate_page_timeout():
    with patch("dev.link_replacer.requests.get", side_effect=Exception("Timeout")):
        result = fetch_candidate_page("http://slow.com/page")
    assert result is None


# ── compute_similarity ───────────────────────────────────────────────


def test_compute_similarity_identical():
    text = "The quick brown fox jumps over the lazy dog"
    assert compute_similarity(text, text) == 1.0


def test_compute_similarity_completely_different():
    assert compute_similarity("alpha beta gamma", "one two three") == 0.0


def test_compute_similarity_partial_overlap():
    a = "The climate change report discusses rising temperatures in South America"
    b = "Rising temperatures in South America are discussed in the new climate report"
    score = compute_similarity(a, b)
    assert 0.5 < score < 1.0


def test_compute_similarity_empty_strings():
    assert compute_similarity("", "") == 0.0
    assert compute_similarity("hello", "") == 0.0


# ── find_live_replacement (integration) ──────────────────────────────


def test_find_live_replacement_via_redirect():
    """If the URL redirects, return immediately without Google/Wayback."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.url = "http://newdomain.gob.mx/doc"
    mock_resp.history = [MagicMock()]  # Had redirects

    with patch("dev.link_replacer.requests.head", return_value=mock_resp):
        result = find_live_replacement("http://old.gob.mx/doc")

    assert result is not None
    assert result["replacement_url"] == "http://newdomain.gob.mx/doc"
    assert result["source"] == "redirect"


def test_find_live_replacement_no_redirect_tries_wayback_then_google():
    """Full pipeline: redirect fails → wayback content → phrase search."""
    # Mock redirect check (no redirect)
    head_resp = MagicMock()
    head_resp.status_code = 404
    head_resp.url = "http://dead.com/page"
    head_resp.history = []

    # Mock Wayback availability
    wayback_avail = MagicMock()
    wayback_avail.status_code = 200
    wayback_avail.json.return_value = {
        "archived_snapshots": {
            "closest": {
                "available": True,
                "url": "http://web.archive.org/web/20220101/http://dead.com/page",
                "timestamp": "20220101",
                "status": "200",
            }
        }
    }

    # Mock Wayback snapshot page
    wayback_page = MagicMock()
    wayback_page.status_code = 200
    wayback_page.text = (
        "<html><head><title>Important Research Paper</title></head>"
        "<body><p>This comprehensive study examines the effects of deforestation "
        "on biodiversity in the Amazon rainforest region over the past decade.</p></body></html>"
    )

    # Mock Tavily search results
    mock_tavily = MagicMock()
    mock_tavily.search.return_value = {
        "results": [
            {"title": "Important Research Paper", "url": "http://newsite.org/paper",
             "content": "effects of deforestation on biodiversity"},
        ]
    }

    # Mock candidate page fetch
    candidate_resp = MagicMock()
    candidate_resp.status_code = 200
    candidate_resp.url = "http://newsite.org/paper"
    candidate_resp.text = (
        "<html><head><title>Important Research Paper</title></head>"
        "<body><p>This comprehensive study examines the effects of deforestation "
        "on biodiversity in the Amazon rainforest region over the past decade.</p></body></html>"
    )

    with _patch_tavily_key():
        with patch("dev.link_replacer.TavilyClient", return_value=mock_tavily):
            with patch("dev.link_replacer.requests.head", return_value=head_resp):
                with patch("dev.link_replacer.requests.get",
                           side_effect=[wayback_avail, wayback_page, candidate_resp]):
                    result = find_live_replacement("http://dead.com/page")

    assert result is not None
    assert result["replacement_url"] == "http://newsite.org/paper"
    assert result["source"] == "google_phrase_match"
    assert result["similarity_score"] > 0.5


def test_find_live_replacement_returns_none_when_all_fail():
    """Returns None when no replacement found."""
    head_resp = MagicMock()
    head_resp.status_code = 404
    head_resp.url = "http://gone.com/x"
    head_resp.history = []

    wayback_resp = MagicMock()
    wayback_resp.status_code = 200
    wayback_resp.json.return_value = {"archived_snapshots": {}}

    mock_tavily = MagicMock()
    mock_tavily.search.return_value = {"results": []}

    with _patch_tavily_key():
        with patch("dev.link_replacer.TavilyClient", return_value=mock_tavily):
            with patch("dev.link_replacer.requests.head", return_value=head_resp):
                with patch("dev.link_replacer.requests.get", return_value=wayback_resp):
                    result = find_live_replacement("http://gone.com/x", page_title="Some Article")

    assert result is None
