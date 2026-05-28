# wp_links

Wikipedia broken-link fixer for es.wikipedia.org. Operates 20 accounts that build credibility by fixing link rot, then transition to warm editors for lead magnet pages.

## Architecture

```
dev/
  db.py               SQLite schema + CRUD (accounts, pages, broken_links, edits)
  fingerprint.py      Per-account Camoufox fingerprint generation
  vpn.py              WireGuard VPN context manager (LAN-safe)
  wiki_browser.py     Camoufox wrapper for Wikipedia login, wikitext, edits
  link_finder.py      Broken link discovery (Wikipedia reports + SemRush)
  link_validator.py   Replacement URL finding (redirects, Wayback Machine)
  edit_engine.py      Typo fixes (warmup) + link fix logic
  account_creator.py  Wikipedia account creation with CAPTCHA solving (claude -p)
  seopack_poc.py      seopack.org automation POC (SemRush/Majestic access)
  slack_notifier.py   Daily Slack reports via webhook
  diagnostics.py      claude -p failure analysis
  orchestrator.py     Daily runner: pick accounts, VPN rotate, edit, report
  setup_accounts.py   Populate DB from accounts.json
  run_daily_cron.sh   Cron wrapper
  data/
    typo_patterns.json  Spanish accent typo patterns for warmup edits
    accounts.json       Account credentials + VPN mapping (gitignored)
  profiles/             Per-account fingerprint + browser profiles (gitignored)
  tests/                Unit tests (74 tests)
wireguard_confs/        WireGuard .conf files (1 per account, gitignored)
```

## Setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
pip install -U "camoufox[geoip]"   # for residential proxy support
python -m camoufox fetch
apt install wireguard-tools         # for VPN rotation during edits
```

Fill in `.env`:
```
SEOPACK_URL=https://seopack.org
SEOPACK_USERNAME=...
SEOPACK_PASSWORD=...
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...
RAYOBYTE_PROXY_HOST=la.residential.rayobyte.com
RAYOBYTE_PROXY_PORT=8000
RAYOBYTE_PROXY_USER=...
RAYOBYTE_PROXY_PASS=...
```

## Account Creation

Wikipedia blocks VPN/datacenter IPs for account creation. Use **Rayobyte residential proxies** for account creation.

```bash
# Populate DB from accounts.json (generates fingerprint profiles too)
python -m dev.setup_accounts dev/data/accounts.json

# Create Wikipedia accounts (uses residential proxy + claude -p for CAPTCHAs)
python -m dev.account_creator --create-all

# Or create one at a time
python -m dev.account_creator --create CarlosWikiES
```

**CAPTCHA handling**: The account creator extracts the CAPTCHA image and uses `claude -p` (with vision) to read it. Success rate is ~50% per attempt.

### Per-account proxy config

Each account has a fixed residential proxy location stored in `connection_config` (JSON) in the DB. The proxy is used for both account creation AND daily edits, ensuring a consistent geographic identity.

`accounts.json` format:
```json
{"username": "...", "password": "...", "proxy": {"country": "MX", "region": "jalisco", "city": "guadalajara"}}
```

Rayobyte proxy format — geo params go in the **password**:
```
username: hallthisis_gmail_com
password: {PROXY_PASS}-country-{CC}[-region-{R}][-city-{C}][-session-{username}]
```

Working locations (tested 2026-05-15):
- MX: nuevo_león, jalisco/guadalajara, puebla/puebla_city, baja_california/tijuana, querétaro, quintana_roo
- CO: antioquia/medellín, valle_del_cauca_department/cali, country-only
- AR: cordoba, country-only
- CL: santiago_metropolitan/santiago
- PE: lima_province

## Typo Finding Strategy

Random article browsing is too slow for finding accent typos. Use the **MediaWiki API** for discovery, then **Camoufox** only for the actual edit:

```python
import requests
r = requests.get('https://es.wikipedia.org/w/api.php', params={
    'action': 'query', 'list': 'search',
    'srsearch': 'insource:"articulo"',  # missing accent
    'srnamespace': 0, 'srlimit': 20, 'format': 'json',
}, headers={'User-Agent': 'WpLinksBot/1.0'})
```

This finds articles in seconds vs. minutes of random browsing.

## SEOPack.org (SemRush/Majestic)

SEOPack is at **seopack.org** (NOT seopack.com which is a domain for sale).

Login flow:
1. `POST https://seopack.org/v2/login/` (fields: `usuario`, `senha`)
2. Dashboard: `https://seopack.org/en/v2/dashboard/`
3. Click "Access SemRush" → opens `smr.seopacktools.com` (30 queries/day)
4. Click "Access Majestic" → opens `mj.seopacktools.com` (15 searches/day)

## VPN Safety

WireGuard configs use `AllowedIPs = 0.0.0.0/0` which routes ALL traffic through the tunnel. The `vpn.py` module preserves SSH/LAN connectivity by:
1. Detecting the current default gateway before VPN activation
2. Adding a static route for the VPN endpoint via the original gateway
3. Preserving the LAN subnet route
4. Cleaning up routes on teardown

## Wikipedia Browser Notes

- Login redirects to `auth.wikimedia.org` — use `domcontentloaded` with 60s timeout
- Edit pages may show a VisualEditor welcome dialog — dismissed automatically
- Large articles (>50KB) can timeout on save — the `expect_navigation` wrapper handles this
- Use `wait_until="load"` for most pages, `"domcontentloaded"` for login/edit

## Daily Operation

1. Orchestrator selects 4 random accounts (weighted to least-recently-used)
2. Each account sequentially: activate VPN → launch Camoufox → login → execute 1 edit → close
3. Warmup accounts (< 2 edits) do typo fixes; active accounts do link fixes
4. 5-15 min random delay between accounts
5. Daily summary posted to Slack
6. On failure: `claude -p` diagnostic spawned, analysis posted to Slack

## Tests

```bash
source venv/bin/activate
python -m pytest dev/tests/ -v   # 74 tests
```

## Deploy Key

Push uses the deploy key at `/opt/projects/wp_links/.deploy_key` via the `github-wp_links` SSH host alias.
