"""SQLite database layer for wp_links.

Manages accounts, pages, broken_links, and edits tables.
"""

import sqlite3
from datetime import datetime, timezone

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
    state TEXT DEFAULT 'warmup',
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
    return conn


# ── accounts ──────────────────────────────────────────────────────────


def add_account(
    conn: sqlite3.Connection,
    username: str,
    password: str,
    vpn_conf_path: str,
    fingerprint_json: str,
    profile_dir: str,
    connection_type: str = "vpn",
    connection_config: str = "",
) -> int:
    cur = conn.execute(
        "INSERT INTO accounts (username, password, vpn_conf_path, fingerprint_json, profile_dir, "
        "connection_type, connection_config, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (username, password, vpn_conf_path, fingerprint_json, profile_dir,
         connection_type, connection_config, _now()),
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


def increment_edit_count(conn: sqlite3.Connection, username: str) -> None:
    conn.execute(
        "UPDATE accounts SET edit_count = edit_count + 1, last_edit_at = ? WHERE username = ?",
        (_now(), username),
    )
    conn.commit()


# ── pages ─────────────────────────────────────────────────────────────


def add_page(conn: sqlite3.Connection, wiki_title: str, found_via: str) -> int:
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


# ── broken_links ──────────────────────────────────────────────────────


def add_broken_link(
    conn: sqlite3.Connection,
    page_id: int,
    original_url: str,
    link_status: int,
    source: str,
) -> int:
    cur = conn.execute(
        "INSERT INTO broken_links (page_id, original_url, link_status, source, verified_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (page_id, original_url, link_status, source, _now()),
    )
    conn.commit()
    return cur.lastrowid


def get_fixable_links(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT bl.*, p.wiki_title FROM broken_links bl "
        "JOIN pages p ON bl.page_id = p.id "
        "WHERE bl.replacement_url IS NOT NULL AND bl.confidence = 'high'"
    ).fetchall()


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
