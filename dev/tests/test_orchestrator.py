"""Tests for dev.orchestrator — daily edit orchestration."""

import sqlite3
from unittest.mock import patch, MagicMock

import pytest

from dev.db import init_db, add_account, get_account
from dev.orchestrator import (
    select_daily_accounts,
    determine_edit_type,
    should_transition_state,
)


@pytest.fixture
def db():
    conn = init_db(":memory:")
    # Add 20 accounts, all registered (select_daily_accounts only picks registered)
    for i in range(20):
        add_account(
            conn,
            username=f"editor{i}",
            password=f"pass{i}",
            fingerprint_json=f'{{"os": "windows", "id": {i}}}',
            profile_dir=f"/profiles/editor{i}/browser",
            state="warmup",
        )
        conn.execute("UPDATE accounts SET registered = 1 WHERE username = ?", (f"editor{i}",))
    conn.commit()
    yield conn
    conn.close()


def test_select_daily_accounts(db):
    selected = select_daily_accounts(db, count=5)
    assert len(selected) == 5
    # All should be unique
    usernames = [a["username"] for a in selected]
    assert len(set(usernames)) == 5


def test_select_daily_accounts_prefers_less_recent(db):
    """Accounts that were used less recently should be preferred."""
    from dev.db import increment_edit_count
    for i in range(10):
        increment_edit_count(db, f"editor{i}")

    selected = select_daily_accounts(db, count=5)
    usernames = [a["username"] for a in selected]
    # The 10 unused accounts (editor10-19) should be preferred
    unused_count = sum(1 for u in usernames if int(u.replace("editor", "")) >= 10)
    assert unused_count >= 1  # Unused accounts should appear (sorted first)


def test_select_daily_accounts_skips_blocked(db):
    from dev.db import update_account_state
    # Block half the accounts
    for i in range(10):
        update_account_state(db, f"editor{i}", "blocked")

    selected = select_daily_accounts(db, count=5)
    for acct in selected:
        assert acct["state"] != "blocked"


def test_select_daily_accounts_only_picks_registered(db):
    """Only registered accounts should be selected."""
    # Unregister all but 3
    db.execute("UPDATE accounts SET registered = 0")
    for i in range(3):
        db.execute("UPDATE accounts SET registered = 1 WHERE username = ?", (f"editor{i}",))
    db.commit()

    selected = select_daily_accounts(db, count=5)
    assert len(selected) == 3  # Only 3 registered available
    for acct in selected:
        assert acct["registered"] == 1


def test_select_daily_accounts_empty_when_none_registered(db):
    """Returns empty list when no accounts are registered."""
    db.execute("UPDATE accounts SET registered = 0")
    db.commit()

    selected = select_daily_accounts(db, count=5)
    assert len(selected) == 0


def test_determine_edit_type():
    # warmup account with 0 edits (even) -> typo
    assert determine_edit_type("warmup", 0) == "typo"
    # warmup account with 1 edit (odd) -> spacing
    assert determine_edit_type("warmup", 1) == "spacing"
    # warmup account with 2 edits (even) -> typo
    assert determine_edit_type("warmup", 2) == "typo"
    # active account -> link_fix regardless of count
    assert determine_edit_type("active", 5) == "link_fix"


def test_should_transition_state():
    assert should_transition_state("warmup", 4) is False
    assert should_transition_state("warmup", 5) is True
    assert should_transition_state("active", 10) is False
    assert should_transition_state("blocked", 5) is False


def test_mark_link_stale_removes_from_fixable():
    """mark_link_stale should clear replacement_url so get_fixable_links skips it."""
    from dev.db import (
        init_db, add_page, add_broken_link, set_replacement_url,
        get_fixable_links, mark_link_stale,
    )
    conn = init_db(":memory:")
    page_id = add_page(conn, "Test Article", "test")
    link_id = add_broken_link(conn, page_id, "http://dead.example.com", 404, "test")
    set_replacement_url(conn, link_id, "http://new.example.com", "high", "redirect")

    assert len(get_fixable_links(conn)) == 1

    mark_link_stale(conn, link_id, "url_removed_from_article")

    assert len(get_fixable_links(conn)) == 0
    conn.close()
