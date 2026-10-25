#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
python3 -m venv .venv 2>/dev/null || true
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
export FRAUDGUARD_INGEST_API_KEY="${FRAUDGUARD_INGEST_API_KEY:-local-demo-key}"
echo "FraudGuard Live opens at http://127.0.0.1:8000"
python -m uvicorn app:app --host 127.0.0.1 --port 8000
