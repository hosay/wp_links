"""Initialize accounts in the database.

Reads account credentials from a JSON file and populates the SQLite
database with accounts, each mapped to a VPN config and fingerprint profile.

Usage:
    python -m dev.setup_accounts dev/data/accounts.json
    python -m dev.setup_accounts --generate 20  # generate placeholder file

The accounts JSON format:
[
    {
        "username": "editor0",
        "password": "pass0",
        "vpn_conf": "Chile-1-CL-25.conf"
    },
    ...
]
"""

import json
import logging
import os
import sys

from dev.db import init_db, add_account, get_account
from dev.fingerprint import generate_all_profiles, load_fingerprint

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

DB_PATH = os.path.join(os.path.dirname(__file__), "wp_links.db")
PROFILES_DIR = os.path.join(os.path.dirname(__file__), "profiles")
VPN_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "wireguard_confs")


def setup_accounts(accounts_file: str):
    """Load accounts from JSON and insert into database."""
    with open(accounts_file) as f:
        accounts = json.load(f)

    # Generate fingerprint profiles for all usernames
    usernames = [a["username"] for a in accounts]
    log.info("Generating fingerprint profiles for %d accounts...", len(usernames))
    generate_all_profiles(usernames, PROFILES_DIR)

    # Initialise DB
    conn = init_db(DB_PATH)
    added = 0
    skipped = 0

    for acct in accounts:
        username = acct["username"]
        if get_account(conn, username):
            log.info("Account %s already exists — skipping", username)
            skipped += 1
            continue

        vpn_conf_path = os.path.join(VPN_DIR, acct["vpn_conf"])
        if not os.path.exists(vpn_conf_path):
            log.warning("VPN config not found: %s — skipping %s", vpn_conf_path, username)
            skipped += 1
            continue

        profile_dir = os.path.join(PROFILES_DIR, username, "browser")

        add_account(
            conn,
            username=username,
            password=acct["password"],
            vpn_conf_path=vpn_conf_path,
            fingerprint_json=json.dumps(load_fingerprint(username, PROFILES_DIR)),
            profile_dir=profile_dir,
        )
        added += 1
        log.info("Added account: %s (vpn: %s)", username, acct["vpn_conf"])

    conn.close()
    log.info("Done: %d added, %d skipped", added, skipped)


def generate_placeholder(count: int):
    """Generate a placeholder accounts.json for the user to fill in."""
    vpn_files = sorted(os.listdir(VPN_DIR)) if os.path.isdir(VPN_DIR) else []
    accounts = []
    for i in range(count):
        vpn = vpn_files[i] if i < len(vpn_files) else f"vpn_{i}.conf"
        accounts.append({
            "username": f"editor{i}",
            "password": f"CHANGE_ME_{i}",
            "vpn_conf": vpn,
        })

    out_path = os.path.join(os.path.dirname(__file__), "data", "accounts.json")
    with open(out_path, "w") as f:
        json.dump(accounts, f, indent=2)
    log.info("Generated placeholder: %s — edit passwords before running setup", out_path)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python -m dev.setup_accounts <accounts.json>")
        print("       python -m dev.setup_accounts --generate 20")
        sys.exit(1)

    if sys.argv[1] == "--generate":
        count = int(sys.argv[2]) if len(sys.argv) > 2 else 20
        generate_placeholder(count)
    else:
        setup_accounts(sys.argv[1])
