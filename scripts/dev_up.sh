#!/usr/bin/env bash
# Start both mock providers and the gateway. Ctrl-C stops everything.
#   ./scripts/dev_up.sh            (add RESET_DB=1 to start from an empty database)
set -e
cd "$(dirname "$0")/.."
[ "${RESET_DB:-0}" = "1" ] && rm -f prism.sqlite3 prism.sqlite3-shm prism.sqlite3-wal
python3 scripts/mock_provider.py --port 9001 --name alpha & P1=$!
python3 scripts/mock_provider.py --port 9002 --name beta & P2=$!
trap 'kill $P1 $P2 2>/dev/null' EXIT
sleep 1
python3 -m uvicorn prism.app:app --port "${PORT:-8080}"
