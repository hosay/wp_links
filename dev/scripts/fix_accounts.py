"""One-shot script to fix account states.

- Move all 19 "pending" accounts → "warmup"
- Reset CarlosWikiES to "warmup" with edit_count=0

Usage:
    python -m dev.scripts.fix_accounts
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from dev.db import init_db, get_accounts_by_state, update_account_state

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "wp_links.db")


def main():
    conn = init_db(DB_PATH)

    # Move pending → warmup
    pending = get_accounts_by_state(conn, "pending")
    print(f"Found {len(pending)} pending accounts")
    for acct in pending:
        update_account_state(conn, acct["username"], "warmup")
        print(f"  {acct['username']} → warmup")

    # Reset CarlosWikiES
    conn.execute(
        "UPDATE accounts SET state = 'warmup', edit_count = 0 WHERE username = 'CarlosWikiES'"
    )
    conn.commit()
    print("Reset CarlosWikiES → warmup, edit_count=0")

    # Summary
    warmup = get_accounts_by_state(conn, "warmup")
    active = get_accounts_by_state(conn, "active")
    print(f"\nFinal state: {len(warmup)} warmup, {len(active)} active")
    conn.close()


if __name__ == "__main__":
    main()
