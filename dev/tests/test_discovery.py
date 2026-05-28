"""Tests for dev.discovery — standalone broken link discovery pipeline."""

from unittest.mock import patch, MagicMock

import pytest

from dev.db import init_db, get_fixable_links, get_broken_links_needing_replacement
from dev.discovery import discover_broken_links


@pytest.fixture
def db():
    conn = init_db(":memory:")
    yield conn
    conn.close()


def test_discover_broken_links_full_pipeline(db):
    """End-to-end discovery: category → wikitext → parse → replace."""
    # Mock category API
    mock_category = (["Artículo_Test"], None)

    # Mock wikitext batch
    mock_wikitext = {
        "Artículo_Test": '{{cita web |url=http://dead.org/doc |título=Test |urlmuerta=sí}}'
    }

    # Mock replacement finding
    mock_replacement = {
        "replacement_url": "http://newsite.org/doc",
        "source": "google_phrase_match",
        "similarity_score": 0.85,
        "wayback_snapshot_url": "http://web.archive.org/web/2023/http://dead.org/doc",
        "search_query": '"some distinctive phrase"',
    }

    mock_liveness = {"alive": True, "soft_404": False, "title": "Test", "text": "content"}
    mock_content = {"is_relevant": True, "reasoning": "matches"}

    with patch("dev.discovery.fetch_category_members_api", return_value=mock_category):
        with patch("dev.discovery.fetch_wikitext_batch_api", return_value=mock_wikitext):
            with patch("dev.discovery.find_live_replacement", return_value=mock_replacement):
                with patch("dev.discovery.verify_replacement_live", return_value=mock_liveness):
                    with patch("dev.discovery.verify_replacement_content", return_value=mock_content):
                        stats = discover_broken_links(db, max_articles=10)

    assert stats["articles_checked"] == 1
    assert stats["broken_urls_found"] == 1
    assert stats["replacements_found"] == 1

    # Check DB has the fixable link
    fixable = get_fixable_links(db)
    assert len(fixable) == 1
    assert fixable[0]["replacement_url"] == "http://newsite.org/doc"
    assert fixable[0]["confidence"] in ("high", "medium")


def test_discover_broken_links_no_replacement(db):
    """Discovery records broken link even when no replacement found."""
    mock_category = (["Art1"], None)
    mock_wikitext = {
        "Art1": '{{enlace roto |url=http://gone.com/x}}'
    }

    with patch("dev.discovery.fetch_category_members_api", return_value=mock_category):
        with patch("dev.discovery.fetch_wikitext_batch_api", return_value=mock_wikitext):
            with patch("dev.discovery.find_live_replacement", return_value=None):
                stats = discover_broken_links(db, max_articles=5)

    assert stats["broken_urls_found"] == 1
    assert stats["replacements_found"] == 0

    # Link is in DB but marked as searched (no longer "needing" — won't waste credits next run)
    row = db.execute("SELECT * FROM broken_links WHERE original_url = 'http://gone.com/x'").fetchone()
    assert row is not None
    assert row["replacement_url"] is None
    assert row["search_query"] is not None  # marked as searched


def test_discover_broken_links_empty_category(db):
    """Handles empty category gracefully."""
    with patch("dev.discovery.fetch_category_members_api", return_value=([], None)):
        stats = discover_broken_links(db, max_articles=10)

    assert stats["articles_checked"] == 0
    assert stats["broken_urls_found"] == 0
