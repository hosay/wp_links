"""Failure diagnostics via claude -p.

When the edit process fails, spawns a Claude instance to analyse the
error and produce a short report suitable for posting to Slack.
Follows the pattern from /opt/projects/xflippa/dev/run_with_notify.py.
"""

import logging
import subprocess

log = logging.getLogger(__name__)

DIAGNOSTIC_TIMEOUT = 180  # seconds


def build_diagnostic_prompt(
    account: str,
    edit_type: str,
    error: str,
    page_title: str,
) -> str:
    """Build the prompt for the diagnostic claude -p call."""
    return f"""A Wikipedia edit bot encountered an error. Analyse the issue and suggest a fix.

Account: {account}
Edit type: {edit_type}
Target page: {page_title}
Error: {error}

Steps to diagnose:
1. Check if es.wikipedia.org is accessible
2. Check if the account might be blocked (look for block messages)
3. Check if the page is protected
4. Check if there's a CAPTCHA or rate limit
5. Check if the VPN connection is working

Report the root cause and suggested fix in 2-3 sentences. Be specific."""


def run_diagnostic(
    account: str,
    edit_type: str,
    error: str,
    page_title: str,
) -> str:
    """Spawn claude -p to analyse a failure. Returns the analysis string."""
    prompt = build_diagnostic_prompt(account, edit_type, error, page_title)
    log.info("Running diagnostic for %s on %s...", account, page_title)

    try:
        result = subprocess.run(
            ["claude", "-p", prompt, "--allowed-tools", "Bash",
             "--dangerously-skip-permissions"],
            capture_output=True,
            text=True,
            timeout=DIAGNOSTIC_TIMEOUT,
            cwd="/opt/projects/wp_links",
        )
        analysis = result.stdout.strip()
        if not analysis:
            analysis = f"Diagnostic returned no output. stderr: {result.stderr[:300]}"
        log.info("Diagnostic complete: %s", analysis[:200])
        return analysis

    except subprocess.TimeoutExpired:
        msg = f"Diagnostic timed out after {DIAGNOSTIC_TIMEOUT}s"
        log.warning(msg)
        return msg

    except Exception as e:
        msg = f"Diagnostic failed: {e}"
        log.warning(msg)
        return msg
