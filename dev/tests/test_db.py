"""Tests for dev.db — SQLite schema and CRUD operations."""

import sqlite3
from datetime import datetime, timezone

import pytest

from dev.db import (
    init_db,
    add_account,
    get_account,
    get_accounts_by_state,
    update_account_state,
    increment_edit_count,
    add_page,
    get_pending_pages,
    claim_page,
    mark_page_done,
    add_broken_link,
    get_fixable_links,
    set_replacement_url,
    add_edit,
    update_edit_status,
    get_edits_for_account,
    get_daily_summary,
)


@pytest.fixture
def db():
    """In-memory SQLite database, freshly initialised."""
    conn = init_db(":memory:")
    yield conn
    conn.close()


# ── accounts ──────────────────────────────────────────────────────────


def test_add_and_get_account(db):
    add_account(
        db,
        username="editor1",
        password="pass1",
        fingerprint_json='{"os": "windows"}',
        profile_dir="/profiles/editor1/browser",
        connection_config='{"country": "MX"}',
    )
    acct = get_account(db, "editor1")
    assert acct["username"] == "editor1"
    assert acct["password"] == "pass1"
    assert acct["fingerprint_json"] == '{"os": "windows"}'
    assert acct["profile_dir"] == "/profiles/editor1/browser"
    assert acct["edit_count"] == 0
    assert acct["state"] == "pending"


def test_add_duplicate_account_raises(db):
    add_account(db, "editor1", "p", "{}", "/pr")
    with pytest.raises(sqlite3.IntegrityError):
        add_account(db, "editor1", "p2", "{}", "/pr2")


def test_get_accounts_by_state(db):
    add_account(db, "e1", "p", "{}", "/pr1", state="warmup")
    add_account(db, "e2", "p", "{}", "/pr2")
    update_account_state(db, "e2", "active")
    warmup = get_accounts_by_state(db, "warmup")
    active = get_accounts_by_state(db, "active")
    assert len(warmup) == 1
    assert warmup[0]["username"] == "e1"
    assert len(active) == 1
    assert active[0]["username"] == "e2"


def test_increment_edit_count(db):
    add_account(db, "e1", "p", "{}", "/pr")
    increment_edit_count(db, "e1")
    assert get_account(db, "e1")["edit_count"] == 1
    increment_edit_count(db, "e1")
    assert get_account(db, "e1")["edit_count"] == 2


def test_auto_transition_warmup_to_active(db):
    """After 2 edits the account should still be warmup — orchestrator decides transition."""
    add_account(db, "e1", "p", "{}", "/pr", state="warmup")
    increment_edit_count(db, "e1")
    increment_edit_count(db, "e1")
    # db layer does NOT auto-transition; orchestrator calls update_account_state
    assert get_account(db, "e1")["state"] == "warmup"


# ── pages ─────────────────────────────────────────────────────────────


def test_add_and_get_pending_pages(db):
    add_page(db, wiki_title="Ejemplo", found_via="wp_report")
    add_page(db, wiki_title="Prueba", found_via="seopack")
    pending = get_pending_pages(db)
    assert len(pending) == 2
    titles = {p["wiki_title"] for p in pending}
    assert titles == {"Ejemplo", "Prueba"}


def test_claim_and_complete_page(db):
    add_account(db, "e1", "p", "{}", "/pr")
    add_page(db, wiki_title="Ejemplo", found_via="wp_report")
    pages = get_pending_pages(db)
    page_id = pages[0]["id"]
    acct = get_account(db, "e1")

    claim_page(db, page_id, acct["id"])
    # After claim, page should no longer appear in pending
    assert len(get_pending_pages(db)) == 0

    mark_page_done(db, page_id)
    # Verify status changed
    row = db.execute("SELECT status FROM pages WHERE id = ?", (page_id,)).fetchone()
    assert row["status"] == "done"


# ── broken_links ──────────────────────────────────────────────────────


def test_add_broken_link_and_set_replacement(db):
    add_page(db, wiki_title="Ejemplo", found_via="wp_report")
    page_id = get_pending_pages(db)[0]["id"]

    add_broken_link(db, page_id=page_id, original_url="http://old.gob.mx/doc",
                    link_status=404, source="wp_report")
    links = get_fixable_links(db)
    assert len(links) == 0  # no replacement yet

    bl_id = db.execute("SELECT id FROM broken_links WHERE page_id = ?", (page_id,)).fetchone()["id"]
    set_replacement_url(db, bl_id, "http://new.gob.mx/doc", confidence="high", source="redirect")

    links = get_fixable_links(db)
    assert len(links) == 1
    assert links[0]["replacement_url"] == "http://new.gob.mx/doc"
    assert links[0]["confidence"] == "high"


def test_get_fixable_links_high_and_medium_confidence(db):
    """get_fixable_links returns both high and medium confidence, not low."""
    add_page(db, wiki_title="Ejemplo", found_via="wp_report")
    page_id = get_pending_pages(db)[0]["id"]

    add_broken_link(db, page_id=page_id, original_url="http://a.com", link_status=404, source="wp_report")
    add_broken_link(db, page_id=page_id, original_url="http://b.com", link_status=404, source="wp_report")
    add_broken_link(db, page_id=page_id, original_url="http://c.com", link_status=404, source="wp_report")
    bl_ids = [r["id"] for r in db.execute("SELECT id FROM broken_links WHERE page_id = ?", (page_id,)).fetchall()]

    set_replacement_url(db, bl_ids[0], "http://a-new.com", confidence="high", source="redirect")
    set_replacement_url(db, bl_ids[1], "http://b-new.com", confidence="medium", source="google_phrase_match")
    set_replacement_url(db, bl_ids[2], "http://c-new.com", confidence="low", source="manual")

    links = get_fixable_links(db)
    assert len(links) == 2
    urls = {l["original_url"] for l in links}
    assert urls == {"http://a.com", "http://b.com"}


def test_add_broken_link_dedup(db):
    """Duplicate (page_id, original_url) returns existing ID without inserting."""
    add_page(db, wiki_title="Ejemplo", found_via="wp_report")
    page_id = get_pending_pages(db)[0]["id"]

    id1 = add_broken_link(db, page_id=page_id, original_url="http://dupe.com",
                          link_status=404, source="wp_report")
    id2 = add_broken_link(db, page_id=page_id, original_url="http://dupe.com",
                          link_status=404, source="category")
    assert id1 == id2

    count = db.execute("SELECT COUNT(*) as c FROM broken_links WHERE page_id = ?",
                       (page_id,)).fetchone()["c"]
    assert count == 1


def test_get_broken_links_needing_replacement(db):
    from dev.db import get_broken_links_needing_replacement
    add_page(db, wiki_title="Ejemplo", found_via="wp_report")
    page_id = get_pending_pages(db)[0]["id"]

    add_broken_link(db, page_id=page_id, original_url="http://needs.com",
                    link_status=404, source="wp_report")
    add_broken_link(db, page_id=page_id, original_url="http://has.com",
                    link_status=404, source="wp_report")
    bl_ids = [r["id"] for r in db.execute("SELECT id FROM broken_links").fetchall()]
    set_replacement_url(db, bl_ids[1], "http://replaced.com", confidence="high", source="redirect")

    needing = get_broken_links_needing_replacement(db)
    assert len(needing) == 1
    assert needing[0]["original_url"] == "http://needs.com"


def test_schema_migration_adds_columns(db):
    """init_db should add new columns (wayback_snapshot_url, etc.) without error."""
    # Columns should exist after init_db
    row = db.execute("PRAGMA table_info(broken_links)").fetchall()
    col_names = {r["name"] for r in row}
    assert "wayback_snapshot_url" in col_names
    assert "search_query" in col_names
    assert "similarity_score" in col_names
    assert "discovery_method" in col_names


# ── edits ─────────────────────────────────────────────────────────────


def test_add_and_update_edit(db):
    add_account(db, "e1", "p", "{}", "/pr")
    add_page(db, wiki_title="Ejemplo", found_via="wp_report")
    acct = get_account(db, "e1")
    page_id = get_pending_pages(db)[0]["id"]

    edit_id = add_edit(db, account_id=acct["id"], page_id=page_id, edit_type="typo",
                       diff_summary="Fixed accent on artículo")
    assert edit_id is not None

    update_edit_status(db, edit_id, status="success", wp_revision_id="12345")
    edits = get_edits_for_account(db, acct["id"])
    assert len(edits) == 1
    assert edits[0]["status"] == "success"
    assert edits[0]["wp_revision_id"] == "12345"


def test_update_edit_revert(db):
    add_account(db, "e1", "p", "{}", "/pr")
    add_page(db, wiki_title="Ejemplo", found_via="wp_report")
    acct = get_account(db, "e1")
    page_id = get_pending_pages(db)[0]["id"]

    edit_id = add_edit(db, account_id=acct["id"], page_id=page_id, edit_type="link_fix",
                       diff_summary="Replaced dead link")
    update_edit_status(db, edit_id, status="reverted", revert_reason="editor_revert")
    edits = get_edits_for_account(db, acct["id"])
    assert edits[0]["status"] == "reverted"
    assert edits[0]["revert_reason"] == "editor_revert"


# ── daily summary ─────────────────────────────────────────────────────


def test_daily_summary(db):
    add_account(db, "e1", "p", "{}", "/pr")
    add_page(db, wiki_title="Ejemplo", found_via="wp_report")
    acct = get_account(db, "e1")
    page_id = get_pending_pages(db)[0]["id"]

    edit_id = add_edit(db, account_id=acct["id"], page_id=page_id, edit_type="typo",
                       diff_summary="Fixed accent")
    update_edit_status(db, edit_id, status="success")

    summary = get_daily_summary(db)
    assert summary["total_edits"] >= 1
    assert summary["successful_edits"] >= 1
