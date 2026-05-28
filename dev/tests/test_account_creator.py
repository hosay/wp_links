"""Tests for dev.account_creator — account creation with block handling and CAPTCHA retry."""

import json
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock, PropertyMock

import pytest

from dev.db import (
    init_db,
    add_account,
    get_account,
    mark_account_blocked,
    get_block_count,
    reassign_proxy,
)


@pytest.fixture
def db():
    conn = init_db(":memory:")
    yield conn
    conn.close()


# ── DB helpers for block tracking ────────────────────────────────────


def test_mark_account_blocked_sets_blocked_until(db):
    add_account(db, "editor1", "pass", "{}", "/pr",
                connection_config='{"country": "CO", "region": "cali"}')
    mark_account_blocked(db, "editor1", hours=24)
    acct = get_account(db, "editor1")
    assert acct["blocked_until"] is not None
    blocked_until = datetime.fromisoformat(acct["blocked_until"])
    # Should be ~24h from now
    expected = datetime.now(timezone.utc) + timedelta(hours=24)
    assert abs((blocked_until - expected).total_seconds()) < 60


def test_mark_account_blocked_increments_block_count(db):
    add_account(db, "editor1", "pass", "{}", "/pr",
                connection_config='{"country": "CO"}')
    assert get_block_count(db, "editor1") == 0
    mark_account_blocked(db, "editor1", hours=24)
    assert get_block_count(db, "editor1") == 1
    mark_account_blocked(db, "editor1", hours=36)
    assert get_block_count(db, "editor1") == 2


def test_block_count_escalates_backoff(db):
    """1st block=24h, 2nd=36h, 3rd=48h."""
    add_account(db, "editor1", "pass", "{}", "/pr",
                connection_config='{"country": "CO"}')

    from dev.account_creator import get_backoff_hours
    assert get_backoff_hours(0) == 24
    assert get_backoff_hours(1) == 36
    assert get_backoff_hours(2) == 48
    # Caps at 48h
    assert get_backoff_hours(5) == 48


def test_reassign_proxy_updates_connection_config(db):
    add_account(db, "editor1", "pass", "{}", "/pr",
                connection_config='{"country": "CO", "region": "cali"}')
    new_proxy = {"country": "MX", "region": "mexico_city"}
    reassign_proxy(db, "editor1", new_proxy)
    acct = get_account(db, "editor1")
    assert json.loads(acct["connection_config"]) == new_proxy


def test_proxy_reassigned_after_3_blocks(db):
    """After 3 blocks, proxy should be reassigned to MX/mexico_city."""
    add_account(db, "editor1", "pass", "{}", "/pr",
                connection_config='{"country": "CO", "region": "cali"}')

    from dev.account_creator import get_fallback_proxy
    proxy = json.loads(get_account(db, "editor1")["connection_config"])
    fallback = get_fallback_proxy(proxy)
    assert fallback == {"country": "MX", "region": "mexico_city"}


def test_proxy_reassigned_to_madrid_if_already_mx(db):
    """If already MX, fall back to ES/madrid."""
    add_account(db, "editor1", "pass", "{}", "/pr",
                connection_config='{"country": "MX", "region": "mexico_city"}')

    from dev.account_creator import get_fallback_proxy
    proxy = json.loads(get_account(db, "editor1")["connection_config"])
    fallback = get_fallback_proxy(proxy)
    assert fallback == {"country": "ES", "region": "madrid"}


def test_no_fallback_if_already_madrid(db):
    """If already ES/madrid, no further fallback — returns None."""
    add_account(db, "editor1", "pass", "{}", "/pr",
                connection_config='{"country": "ES", "region": "madrid"}')

    from dev.account_creator import get_fallback_proxy
    proxy = json.loads(get_account(db, "editor1")["connection_config"])
    fallback = get_fallback_proxy(proxy)
    assert fallback is None


# ── Orchestrator: blocked account filtering ──────────────────────────


def test_blocked_account_skipped_when_blocked_until_active(db):
    """Accounts with future blocked_until should not be selected."""
    from dev.orchestrator import select_daily_accounts

    for i in range(5):
        add_account(db, f"editor{i}", "pass", "{}", f"/pr{i}", state="warmup")
        db.execute("UPDATE accounts SET registered = 1 WHERE username = ?", (f"editor{i}",))
    db.commit()
    # Block editor0 for 24h
    mark_account_blocked(db, "editor0", hours=24)

    selected = select_daily_accounts(db, count=5)
    usernames = [a["username"] for a in selected]
    assert "editor0" not in usernames


def test_blocked_account_retried_after_blocked_until_expires(db):
    """Accounts with past blocked_until should be eligible again."""
    from dev.orchestrator import select_daily_accounts

    add_account(db, "editor0", "pass", "{}", "/pr0", state="warmup")
    db.execute("UPDATE accounts SET registered = 1 WHERE username = ?", ("editor0",))
    # Set blocked_until to 1 hour ago (expired)
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    db.execute("UPDATE accounts SET blocked_until = ? WHERE username = ?",
               (past, "editor0"))
    db.commit()

    selected = select_daily_accounts(db, count=5)
    usernames = [a["username"] for a in selected]
    assert "editor0" in usernames


def test_verify_password_returns_false_on_exception():
    """_verify_password must return False on exception, not True."""
    from dev.account_creator import _verify_password

    with patch("dev.account_creator.http_requests.Session") as mock_session:
        mock_session.return_value.get.side_effect = ConnectionError("proxy down")
        result = _verify_password("testuser", "testpass", {"server": "x", "username": "u", "password": "p"})
        assert result is False
