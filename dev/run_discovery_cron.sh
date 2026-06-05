#!/usr/bin/env bash
# Discovery cron — populates broken_links table with replacement URLs.
# Install: crontab -e
#   0 */6 * * * /opt/projects/wp_links/dev/run_discovery_cron.sh >> /opt/projects/wp_links/dev/discovery.log 2>&1
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

echo "=========================================="
echo "$(date -Iseconds) — Discovery cycle triggered"
echo "=========================================="

cd "$PROJECT_DIR"
source venv/bin/activate

# Load env vars
set -a
source .env
set +a

python -m dev.discovery --max-articles 200

echo "$(date -Iseconds) — Discovery cycle finished"
