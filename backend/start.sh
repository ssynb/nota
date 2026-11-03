#!/bin/bash
# Starts one worker and the API (2 processes) on http://localhost:${PORT:-8077}; Ctrl-C stops all.
set -euo pipefail
cd "$(dirname "$0")"
env_file=()
[ -f ../.env ] && env_file=(--env-file ../.env)
trap 'kill 0' EXIT
uv run "${env_file[@]}" python -m nota.worker &
uv run "${env_file[@]}" uvicorn nota.app:app --host 0.0.0.0 --port "${PORT:-8077}" --workers 2
