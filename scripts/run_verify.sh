#!/usr/bin/env bash
# One-command end-to-end verification (venv must exist; run from repo root).
set -euo pipefail
cd "$(dirname "$0")/.."
.venv/bin/python scripts/make_test_data.py
.venv/bin/python scripts/verify.py
