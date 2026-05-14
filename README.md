# wp_links

Wikipedia broken-link fixer for es.wikipedia.org. Operates 20 accounts that build credibility by fixing link rot, then transition to warm editors for lead magnet pages.

## Architecture

```
dev/
  db.py               SQLite schema + CRUD (accounts, pages, broken_links, edits)
  fingerprint.py      Per-account Camoufox fingerprint generation
  vpn.py              WireGuard VPN context manager
  wiki_browser.py     Camoufox wrapper for Wikipedia login, wikitext, edits
  link_finder.py      Broken link discovery (Wikipedia reports + SemRush)
  link_validator.py   Replacement URL finding (redirects, Wayback Machine)
  edit_engine.py      Typo fixes (warmup) + link fix logic
  seopack_poc.py      seopack.org automation POC (SemRush/Majestic access)
  slack_notifier.py   Daily Slack reports via webhook
  diagnostics.py      claude -p failure analysis
  orchestrator.py     Daily runner: pick accounts, VPN rotate, edit, report
  run_daily_cron.sh   Cron wrapper
  data/
    typo_patterns.json  Spanish accent typo patterns for warmup edits
  profiles/             Per-account fingerprint + browser profiles
  tests/                Unit tests (74 tests)
wireguard_confs/        WireGuard .conf files (1 per account, gitignored)
```

## Setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python -m camoufox fetch
```

Copy `.env.example` to `.env` and fill in credentials:
```
SEOPACK_URL=https://seopack.org
SEOPACK_USERNAME=...
SEOPACK_PASSWORD=...
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...
```

Place 20 WireGuard configs in `wireguard_confs/`.

## Usage

Generate fingerprint profiles (one-time):
```bash
python -c "from dev.fingerprint import generate_all_profiles; generate_all_profiles([f'editor{i}' for i in range(20)], 'dev/profiles')"
```

Run daily edits:
```bash
python -m dev.orchestrator         # production run
python -m dev.orchestrator --dry   # dry run (no actual edits)
```

Install cron (runs daily at 14:00 UTC):
```bash
crontab -e
# Add: 0 14 * * * /opt/projects/wp_links/dev/run_daily_cron.sh >> /opt/projects/wp_links/dev/cron.log 2>&1
```

## Tests

```bash
source venv/bin/activate
python -m pytest dev/tests/ -v
```

## Daily Operation

1. Orchestrator selects 4 random accounts (weighted to least-recently-used)
2. Each account sequentially: activate VPN -> launch Camoufox -> login -> execute 1 edit -> close
3. Warmup accounts (< 2 edits) do typo fixes; active accounts do link fixes
4. 5-15 min random delay between accounts
5. Daily summary posted to Slack
6. On failure: `claude -p` diagnostic spawned, analysis posted to Slack

## Deploy Key

Push uses the deploy key at `/opt/projects/wp_links/.deploy_key` via the `github-wp_links` SSH host alias. See `/root/.ssh/config` for the alias definition.
