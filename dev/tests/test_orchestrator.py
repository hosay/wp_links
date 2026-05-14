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
    # Add 20 accounts
    for i in range(20):
        add_account(
            conn,
            username=f"editor{i}",
            password=f"pass{i}",
            vpn_conf_path=f"/vpn/editor{i}.conf",
            fingerprint_json=f'{{"os": "windows", "id": {i}}}',
            profile_dir=f"/profiles/editor{i}/browser",
        )
    yield conn
    conn.close()


def test_select_daily_accounts(db):
    selected = select_daily_accounts(db, count=4)
    assert len(selected) == 4
    # All should be unique
    usernames = [a["username"] for a in selected]
    assert len(set(usernames)) == 4


def test_select_daily_accounts_prefers_less_recent(db):
    """Accounts that were used less recently should be preferred."""
    # Mark some accounts as recently used
    from dev.db import increment_edit_count
    for i in range(10):
        increment_edit_count(db, f"editor{i}")

    selected = select_daily_accounts(db, count=4)
    usernames = [a["username"] for a in selected]
    # The 10 unused accounts (editor10-19) should be preferred
    unused_count = sum(1 for u in usernames if int(u.replace("editor", "")) >= 10)
    assert unused_count >= 2  # At least half should be from unused pool


def test_select_daily_accounts_skips_blocked(db):
    from dev.db import update_account_state
    # Block half the accounts
    for i in range(10):
        update_account_state(db, f"editor{i}", "blocked")

    selected = select_daily_accounts(db, count=4)
    for acct in selected:
        assert acct["state"] != "blocked"


def test_determine_edit_type():
    # warmup account with 0 edits -> typo
    assert determine_edit_type("warmup", 0) == "typo"
    # warmup account with 1 edit -> typo
    assert determine_edit_type("warmup", 1) == "typo"
    # active account -> link_fix
    assert determine_edit_type("active", 5) == "link_fix"


def test_should_transition_state():
    assert should_transition_state("warmup", 1) is False
    assert should_transition_state("warmup", 2) is True
    assert should_transition_state("active", 10) is False
    assert should_transition_state("blocked", 2) is False
