#!/usr/bin/env bash
# Run the sender on a developer Linux laptop for DR validation.
# Requires a mock PRICES_ROOT laid out with <job>/GetPricesResult/ folders
# and a local .env pointing RECEIVER_URL at http://127.0.0.1:8080/prices.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_ROOT"

if [[ ! -f .env ]]; then
  echo "[ERROR] .env not found. Copy .env.example to .env and edit it first."
  exit 2
fi

if [[ ! -d .venv ]]; then
  python3 -m venv .venv
  .venv/bin/pip install --upgrade pip
  .venv/bin/pip install -r requirements.txt
fi

exec .venv/bin/python -m src.main
