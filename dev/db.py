"""SQLite database layer for wp_links.

Manages accounts, pages, broken_links, and edits tables.
"""

import sqlite3
from datetime import datetime, timedelta, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY,
    username TEXT UNIQUE NOT NULL,
    password TEXT NOT NULL,
    vpn_conf_path TEXT NOT NULL,
    fingerprint_json TEXT NOT NULL,
    profile_dir TEXT NOT NULL,
    connection_type TEXT DEFAULT 'proxy',
    connection_config TEXT DEFAULT '',
    edit_count INTEGER DEFAULT 0,
    state TEXT DEFAULT 'pending',
    created_at TEXT,
    last_edit_at TEXT
);

CREATE TABLE IF NOT EXISTS pages (
    id INTEGER PRIMARY KEY,
    wiki_title TEXT NOT NULL,
    lang TEXT DEFAULT 'es',
    found_via TEXT,
    status TEXT DEFAULT 'pending',
    claimed_by_account INTEGER REFERENCES accounts(id),
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS broken_links (
    id INTEGER PRIMARY KEY,
    page_id INTEGER REFERENCES pages(id),
    original_url TEXT NOT NULL,
    replacement_url TEXT,
    confidence TEXT,
    link_status INTEGER,
    verified_at TEXT,
    source TEXT
);

CREATE TABLE IF NOT EXISTS edits (
    id INTEGER PRIMARY KEY,
    account_id INTEGER REFERENCES accounts(id),
    page_id INTEGER REFERENCES pages(id),
    edit_type TEXT NOT NULL,
    diff_summary TEXT,
    wp_revision_id TEXT,
    status TEXT DEFAULT 'pending',
    revert_reason TEXT,
    error_message TEXT,
    attempted_at TEXT,
    completed_at TEXT
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_db(db_path: str = "dev/wp_links.db") -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    conn.commit()
    _migrate_schema_v2(conn)
    return conn


def _migrate_schema_v2(conn: sqlite3.Connection) -> None:
    """Add new columns to broken_links and accounts tables (idempotent)."""
    bl_cols = [
        ("wayback_snapshot_url", "TEXT"),
        ("search_query", "TEXT"),
        ("similarity_score", "REAL"),
        ("discovery_method", "TEXT"),
    ]
    for col, typ in bl_cols:
        try:
            conn.execute(f"ALTER TABLE broken_links ADD COLUMN {col} {typ}")
        except sqlite3.OperationalError:
            pass
    # Track whether account is registered on Wikipedia
    try:
        conn.execute("ALTER TABLE accounts ADD COLUMN registered INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    # Block tracking: cooldown timestamp and cumulative block count
    for col, typ in [("blocked_until", "TEXT"), ("block_count", "INTEGER DEFAULT 0")]:
        try:
            conn.execute(f"ALTER TABLE accounts ADD COLUMN {col} {typ}")
        except sqlite3.OperationalError:
            pass
    conn.commit()


# ── accounts ──────────────────────────────────────────────────────────


def add_account(
    conn: sqlite3.Connection,
    username: str,
    password: str,
    fingerprint_json: str,
    profile_dir: str,
    connection_type: str = "proxy",
    connection_config: str = "",
    vpn_conf_path: str = "",
    state: str = "pending",
) -> int:
    cur = conn.execute(
        "INSERT INTO accounts (username, password, vpn_conf_path, fingerprint_json, profile_dir, "
        "connection_type, connection_config, state, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (username, password, vpn_conf_path, fingerprint_json, profile_dir,
         connection_type, connection_config, state, _now()),
    )
    conn.commit()
    return cur.lastrowid


def get_account(conn: sqlite3.Connection, username: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM accounts WHERE username = ?", (username,)
    ).fetchone()


def get_accounts_by_state(conn: sqlite3.Connection, state: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM accounts WHERE state = ?", (state,)
    ).fetchall()


def update_account_state(conn: sqlite3.Connection, username: str, new_state: str) -> None:
    conn.execute(
        "UPDATE accounts SET state = ? WHERE username = ?", (new_state, username)
    )
    conn.commit()


def mark_account_blocked(conn: sqlite3.Connection, username: str, hours: int = 24) -> None:
    """Set blocked_until and increment block_count for an account."""
    blocked_until = (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()
    conn.execute(
        "UPDATE accounts SET blocked_until = ?, block_count = COALESCE(block_count, 0) + 1 "
        "WHERE username = ?",
        (blocked_until, username),
    )
    conn.commit()


def get_block_count(conn: sqlite3.Connection, username: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(block_count, 0) as bc FROM accounts WHERE username = ?",
        (username,),
    ).fetchone()
    return row["bc"] if row else 0


def reassign_proxy(conn: sqlite3.Connection, username: str, new_proxy_config: dict) -> None:
    """Update an account's proxy configuration."""
    import json
    conn.execute(
        "UPDATE accounts SET connection_config = ? WHERE username = ?",
        (json.dumps(new_proxy_config), username),
    )
    conn.commit()


def get_all_account_summaries(conn: sqlite3.Connection) -> list[dict]:
    """Get per-account summary: username, state, edit_count, registered, last_edit_at."""
    rows = conn.execute(
        "SELECT username, state, edit_count, registered, last_edit_at, blocked_until "
        "FROM accounts ORDER BY edit_count DESC, username"
    ).fetchall()
    return [dict(r) for r in rows]


def increment_edit_count(conn: sqlite3.Connection, username: str) -> None:
    conn.execute(
        "UPDATE accounts SET edit_count = edit_count + 1, last_edit_at = ? WHERE username = ?",
        (_now(), username),
    )
    conn.commit()


# ── pages ─────────────────────────────────────────────────────────────


def add_page(conn: sqlite3.Connection, wiki_title: str, found_via: str) -> int:
    """Insert a page, deduplicating by wiki_title."""
    existing = conn.execute(
        "SELECT id FROM pages WHERE wiki_title = ?", (wiki_title,)
    ).fetchone()
    if existing:
        return existing["id"]
    cur = conn.execute(
        "INSERT INTO pages (wiki_title, found_via, created_at) VALUES (?, ?, ?)",
        (wiki_title, found_via, _now()),
    )
    conn.commit()
    return cur.lastrowid


def get_pending_pages(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM pages WHERE status = 'pending'"
    ).fetchall()


def claim_page(conn: sqlite3.Connection, page_id: int, account_id: int) -> None:
    conn.execute(
        "UPDATE pages SET status = 'claimed', claimed_by_account = ? WHERE id = ?",
        (account_id, page_id),
    )
    conn.commit()


def mark_page_done(conn: sqlite3.Connection, page_id: int) -> None:
    conn.execute("UPDATE pages SET status = 'done' WHERE id = ?", (page_id,))
    conn.commit()


def mark_page_unclaimed(conn: sqlite3.Connection, page_id: int) -> None:
    """Revert a page to pending so it can be retried on the next run."""
    conn.execute(
        "UPDATE pages SET status = 'pending', claimed_by_account = NULL WHERE id = ?",
        (page_id,),
    )
    conn.commit()


# ── broken_links ──────────────────────────────────────────────────────


def add_broken_link(
    conn: sqlite3.Connection,
    page_id: int,
    original_url: str,
    link_status: int,
    source: str,
    discovery_method: str | None = None,
) -> int:
    """Insert a broken link with dedup on (page_id, original_url)."""
    existing = conn.execute(
        "SELECT id FROM broken_links WHERE page_id = ? AND original_url = ?",
        (page_id, original_url),
    ).fetchone()
    if existing:
        return existing["id"]
    cur = conn.execute(
        "INSERT INTO broken_links (page_id, original_url, link_status, source, "
        "discovery_method, verified_at) VALUES (?, ?, ?, ?, ?, ?)",
        (page_id, original_url, link_status, source, discovery_method, _now()),
    )
    conn.commit()
    return cur.lastrowid


def get_fixable_links(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT bl.*, p.wiki_title FROM broken_links bl "
        "JOIN pages p ON bl.page_id = p.id "
        "WHERE bl.replacement_url IS NOT NULL AND bl.confidence IN ('high', 'medium')"
    ).fetchall()


def get_broken_links_needing_replacement(
    conn: sqlite3.Connection, limit: int = 50
) -> list[sqlite3.Row]:
    """Get broken links that haven't been searched yet.

    Skips links where search_query is set (already searched, no result found).
    """
    return conn.execute(
        "SELECT bl.*, p.wiki_title FROM broken_links bl "
        "JOIN pages p ON bl.page_id = p.id "
        "WHERE bl.replacement_url IS NULL AND bl.search_query IS NULL "
        "ORDER BY bl.verified_at ASC LIMIT ?",
        (limit,),
    ).fetchall()


def mark_link_searched(conn: sqlite3.Connection, broken_link_id: int, query: str) -> None:
    """Mark a broken link as searched (no replacement found)."""
    conn.execute(
        "UPDATE broken_links SET search_query = ?, verified_at = ? WHERE id = ?",
        (query, _now(), broken_link_id),
    )
    conn.commit()


def mark_link_stale(conn: sqlite3.Connection, broken_link_id: int, reason: str) -> None:
    """Mark a broken link's replacement as stale (e.g. URL removed from article).

    Clears replacement_url so get_fixable_links() skips it, and records
    the reason in search_query for diagnostics.
    """
    conn.execute(
        "UPDATE broken_links SET replacement_url = NULL, confidence = NULL, "
        "search_query = ?, verified_at = ? WHERE id = ?",
        (f"stale:{reason}", _now(), broken_link_id),
    )
    conn.commit()


def set_replacement_url(
    conn: sqlite3.Connection,
    broken_link_id: int,
    replacement_url: str,
    confidence: str,
    source: str,
) -> None:
    conn.execute(
        "UPDATE broken_links SET replacement_url = ?, confidence = ?, source = ?, verified_at = ? "
        "WHERE id = ?",
        (replacement_url, confidence, source, _now(), broken_link_id),
    )
    conn.commit()


# ── edits ─────────────────────────────────────────────────────────────


def add_edit(
    conn: sqlite3.Connection,
    account_id: int,
    page_id: int,
    edit_type: str,
    diff_summary: str,
) -> int:
    cur = conn.execute(
        "INSERT INTO edits (account_id, page_id, edit_type, diff_summary, attempted_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (account_id, page_id, edit_type, diff_summary, _now()),
    )
    conn.commit()
    return cur.lastrowid


def update_edit_status(
    conn: sqlite3.Connection,
    edit_id: int,
    status: str,
    wp_revision_id: str | None = None,
    revert_reason: str | None = None,
    error_message: str | None = None,
) -> None:
    conn.execute(
        "UPDATE edits SET status = ?, wp_revision_id = COALESCE(?, wp_revision_id), "
        "revert_reason = COALESCE(?, revert_reason), error_message = COALESCE(?, error_message), "
        "completed_at = ? WHERE id = ?",
        (status, wp_revision_id, revert_reason, error_message, _now(), edit_id),
    )
    conn.commit()


def get_edits_for_account(conn: sqlite3.Connection, account_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM edits WHERE account_id = ? ORDER BY attempted_at DESC",
        (account_id,),
    ).fetchall()


# ── reporting ─────────────────────────────────────────────────────────


def get_health_metrics(conn: sqlite3.Connection, days: int = 7) -> dict:
    """Get edit health metrics for the last N days.

    Only counts edits with a terminal status (success/reverted/failed),
    not pending edits that never completed.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    successful = conn.execute(
        "SELECT COUNT(*) as c FROM edits WHERE attempted_at >= ? AND status = 'success'",
        (cutoff,),
    ).fetchone()["c"]
    reverted = conn.execute(
        "SELECT COUNT(*) as c FROM edits WHERE attempted_at >= ? AND status = 'reverted'",
        (cutoff,),
    ).fetchone()["c"]
    total = successful + reverted
    return {
        "total_7d": total,
        "reverted_7d": reverted,
        "admin_warnings": 0,  # TODO: implement warning detection
    }


def get_account_pipeline_summary(conn: sqlite3.Connection) -> dict:
    """Get account lifecycle summary for reporting."""
    total = conn.execute("SELECT COUNT(*) as c FROM accounts").fetchone()["c"]
    warmup = conn.execute("SELECT COUNT(*) as c FROM accounts WHERE state = 'warmup'").fetchone()["c"]
    active = conn.execute("SELECT COUNT(*) as c FROM accounts WHERE state = 'active'").fetchone()["c"]
    pending = conn.execute("SELECT COUNT(*) as c FROM accounts WHERE state = 'pending'").fetchone()["c"]
    blocked = conn.execute(
        "SELECT COUNT(*) as c FROM accounts WHERE state IN ('blocked', 'suspended')"
    ).fetchone()["c"]
    avg_row = conn.execute(
        "SELECT AVG(edit_count) as avg FROM accounts WHERE state = 'warmup'"
    ).fetchone()
    avg_warmup = round(avg_row["avg"], 1) if avg_row["avg"] else 0.0
    return {
        "total": total,
        "warmup_count": warmup,
        "active_count": active,
        "pending_count": pending,
        "blocked_count": blocked,
        "avg_warmup_progress": avg_warmup,
    }


def get_daily_summary(conn: sqlite3.Connection) -> dict:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    total = conn.execute(
        "SELECT COUNT(*) as c FROM edits WHERE attempted_at LIKE ?", (f"{today}%",)
    ).fetchone()["c"]
    success = conn.execute(
        "SELECT COUNT(*) as c FROM edits WHERE attempted_at LIKE ? AND status = 'success'",
        (f"{today}%",),
    ).fetchone()["c"]
    failed = conn.execute(
        "SELECT COUNT(*) as c FROM edits WHERE attempted_at LIKE ? AND status = 'failed'",
        (f"{today}%",),
    ).fetchone()["c"]
    reverted = conn.execute(
        "SELECT COUNT(*) as c FROM edits WHERE attempted_at LIKE ? AND status = 'reverted'",
        (f"{today}%",),
    ).fetchone()["c"]
    return {
        "date": today,
        "total_edits": total,
        "successful_edits": success,
        "failed_edits": failed,
        "reverted_edits": reverted,
    }
