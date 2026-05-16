"""Tests for dev.link_validator — broken link validation and replacement finding."""

import pytest

from dev.link_validator import (
    extract_domain,
    is_same_org,
    build_wayback_api_url,
    parse_wayback_response,
    classify_confidence,
    generate_edit_summary,
)


def test_extract_domain():
    assert extract_domain("http://www.gob.mx/doc") == "www.gob.mx"
    assert extract_domain("https://example.org/page?q=1") == "example.org"
    assert extract_domain("http://sub.domain.co.uk/path") == "sub.domain.co.uk"


def test_is_same_org_same_domain():
    assert is_same_org("http://gob.mx/old", "http://gob.mx/new") is True


def test_is_same_org_subdomain():
    assert is_same_org("http://www.gob.mx/old", "http://datos.gob.mx/new") is True


def test_is_same_org_different():
    assert is_same_org("http://gob.mx/old", "http://example.com/new") is False


def test_is_same_org_gov_migration():
    # Common: .gob.mx to .gobierno.mx or similar
    assert is_same_org("http://old.gob.mx/doc", "http://old.gobierno.mx/doc") is True


def test_build_wayback_api_url():
    url = build_wayback_api_url("http://example.com/page")
    assert "archive.org" in url
    assert "example.com/page" in url


def test_parse_wayback_response_found():
    # Simulated Wayback CDX API response
    response = {
        "url": "http://example.com/page",
        "archived_snapshots": {
            "closest": {
                "available": True,
                "url": "http://web.archive.org/web/20230101/http://example.com/page",
                "timestamp": "20230101000000",
                "status": "200",
            }
        },
    }
    result = parse_wayback_response(response)
    assert result is not None
    assert "web.archive.org" in result["snapshot_url"]
    assert result["status"] == "200"


def test_parse_wayback_response_not_found():
    response = {"url": "http://dead.com", "archived_snapshots": {}}
    assert parse_wayback_response(response) is None


def test_classify_confidence_same_org_redirect():
    assert classify_confidence(
        original_url="http://old.gob.mx/doc",
        replacement_url="http://new.gob.mx/doc",
        source="redirect",
    ) == "high"


def test_classify_confidence_wayback_same_org():
    assert classify_confidence(
        original_url="http://old.gob.mx/doc",
        replacement_url="http://web.archive.org/web/2023/http://old.gob.mx/doc",
        source="wayback",
    ) == "medium"


def test_classify_confidence_different_org():
    assert classify_confidence(
        original_url="http://old.gob.mx/doc",
        replacement_url="http://totally-different.com/doc",
        source="manual",
    ) == "low"


def test_generate_edit_summary_redirect():
    s = generate_edit_summary("redirect", "http://old.gob.mx", "http://new.gob.mx")
    assert isinstance(s, str)
    assert len(s) > 10
    assert len(s) < 200


def test_generate_edit_summary_varies():
    """Edit summaries should not be identical for repeated calls."""
    summaries = set()
    for _ in range(10):
        s = generate_edit_summary("redirect", "http://a.com", "http://b.com")
        summaries.add(s)
    # With randomisation, we expect at least 2 distinct summaries in 10 tries
    assert len(summaries) >= 2


# ── new source types ─────────────────────────────────────────────────


def test_classify_confidence_google_phrase_match_same_org():
    assert classify_confidence(
        "http://old.gob.mx/doc", "http://new.gob.mx/doc",
        "google_phrase_match", similarity_score=0.6,
    ) == "high"


def test_classify_confidence_google_phrase_match_high_similarity():
    assert classify_confidence(
        "http://old.gob.mx/doc", "http://different.com/doc",
        "google_phrase_match", similarity_score=0.9,
    ) == "high"


def test_classify_confidence_google_phrase_match_medium_similarity():
    assert classify_confidence(
        "http://old.gob.mx/doc", "http://different.com/doc",
        "google_phrase_match", similarity_score=0.7,
    ) == "medium"


def test_classify_confidence_google_title_search_same_org():
    assert classify_confidence(
        "http://old.gob.mx/doc", "http://new.gob.mx/doc",
        "google_title_search", similarity_score=0.75,
    ) == "high"


def test_classify_confidence_google_title_search_medium():
    assert classify_confidence(
        "http://old.com/doc", "http://other.org/doc",
        "google_title_search", similarity_score=0.65,
    ) == "medium"


def test_generate_edit_summary_google_phrase_match():
    s = generate_edit_summary("google_phrase_match", "http://dead.com/x", "http://new.com/x")
    assert isinstance(s, str)
    assert len(s) > 5


def test_generate_edit_summary_google_title_search():
    s = generate_edit_summary("google_title_search", "http://dead.com/x", "http://new.com/x")
    assert isinstance(s, str)
    assert len(s) > 5
