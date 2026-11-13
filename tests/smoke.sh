#!/usr/bin/env bash
# End-to-end smoke test: generate, run every command, verify, then run pytest.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$here"
PY=".venv/bin/python"
[[ -x "$PY" ]] || PY="python3"
DATA="${1:-data_smoke}"
rm -rf "$DATA"
echo "== all =="
"$PY" -m queueops all --data-dir "$DATA"
echo "== verify =="
"$PY" -m queueops verify --data-dir "$DATA"
echo "== pytest =="
"$PY" -m pytest tests -q
echo "SMOKE OK -> $DATA/report.html"
