#!/usr/bin/env bash
# Daily cron wrapper for the Wikipedia link fixer orchestrator.
# Install: crontab -e
#   0 14 * * * /opt/projects/wp_links/dev/run_daily_cron.sh >> /opt/projects/wp_links/dev/cron.log 2>&1
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

echo "=========================================="
echo "$(date -Iseconds) — Starting daily edit cycle"
echo "=========================================="

cd "$PROJECT_DIR"
source venv/bin/activate

# Load env vars
set -a
source .env
set +a

python -m dev.orchestrator

echo "$(date -Iseconds) — Daily edit cycle finished"
