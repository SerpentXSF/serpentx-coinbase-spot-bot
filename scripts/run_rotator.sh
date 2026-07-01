#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p state logs analysis
exec "${PYTHON:-python}" rotate_and_run.py "$@"
