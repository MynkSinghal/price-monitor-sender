#!/usr/bin/env bash
# Launch the local DR dashboard on a developer Linux laptop.
#   ./scripts/run_dashboard.sh            # binds 127.0.0.1:8080
#   ./scripts/run_dashboard.sh 0.0.0.0 9090
set -euo pipefail

HOST="${1:-127.0.0.1}"
PORT="${2:-8080}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_ROOT"

if [[ ! -d .venv ]]; then
  python3 -m venv .venv
  .venv/bin/pip install --upgrade pip
  .venv/bin/pip install -r requirements.txt
fi

exec .venv/bin/python -m dashboard.mock_receiver --host "$HOST" --port "$PORT"
