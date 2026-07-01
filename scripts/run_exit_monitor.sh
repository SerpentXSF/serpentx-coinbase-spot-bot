#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p state logs analysis
exec "${PYTHON:-python}" exit_monitor.py "$@"
