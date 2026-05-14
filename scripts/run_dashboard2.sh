#!/usr/bin/env bash
# Run dashboard2 two-site mock receiver.
#
# Both UKPROD and USPROD senders must point their RECEIVER_URL at this server:
#   RECEIVER_URL=http://<this-machine-ip>:8081/prices
#
# Default: localhost-only on port 8081.
# Pass --host 0.0.0.0 to accept connections from remote senders.
#
# Usage:
#   bash scripts/run_dashboard2.sh                        # local only
#   bash scripts/run_dashboard2.sh --host 0.0.0.0         # all interfaces
#   bash scripts/run_dashboard2.sh --host 0.0.0.0 --port 9000

set -euo pipefail
cd "$(dirname "$0")/.."

python -m dashboard2.mock_receiver "$@"
