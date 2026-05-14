#!/usr/bin/env bash
# Simulator for DR testing.
# Creates success.txt under a mock PRICES_ROOT to trigger watchdog.
#
# Usage:
#   ./scripts/simulate_success.sh <prices_root> <job1> [job2...]
# Example:
#   ./scripts/simulate_success.sh /tmp/mock-prices NSE_IX_1 NSE_IX_2
set -euo pipefail

if [[ $# -lt 2 ]]; then
  echo "Usage: $0 <PRICES_ROOT> <job> [job ...]"
  exit 1
fi

ROOT="$1"
shift

for job in "$@"; do
  dir="$ROOT/$job/GetPricesResult"
  mkdir -p "$dir"
  : > "$dir/success.txt"
  echo "Created $dir/success.txt"
done
