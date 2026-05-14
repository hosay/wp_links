# CLAUDE.md

## Expert subagent review

For any non-trivial feature or fix, use a two-pass review cycle:

1. **Before implementing**: draft a plan, spawn an `Explore` subagent to critique it. Incorporate feedback before writing code.
2. **After implementing**: spawn a second `Explore` subagent to review the final code. Fix anything blocking.

Subagents read files and report findings only — never write code.

## TDD strategy

Always write a failing test first: write → confirm failure → implement → run full suite → refactor.

## venv and dependencies

Always activate the venv before running Python: `source venv/bin/activate`. When adding a package, install the latest version and then record the exact installed version in `requirements.txt` — never guess the version number:

```bash
pip install somepackage
pip show somepackage | grep Version
# add "somepackage==X.Y.Z" to requirements.txt manually
```

Do not run `pip freeze > requirements.txt` — it dumps transitive dependencies.

## Camoufox

Camoufox is a Firefox-based anti-detection browser built on Playwright. Use it for all page fetches to maintain a consistent TLS fingerprint. Do not switch to `requests` mid-session.

**Setup:** `python -m camoufox fetch`

**Basic usage:**
```python
from camoufox.sync_api import Camoufox

with Camoufox(headless=True) as browser:
    page = browser.new_page()
    page.goto("https://example.com", wait_until="load")
    html = page.content()
```

**`wait_until` strategy:** use `"networkidle"` for React-rendered login pages, `"load"` for all other pages (analytics scripts block `networkidle`), and `"domcontentloaded"` for simple pages needing no interaction.

## Git / SSH

Push uses the deploy key:
```bash
GIT_SSH_COMMAND="ssh -i /opt/projects/deploy_key -o StrictHostKeyChecking=no" git push origin main
```
