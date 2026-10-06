#!/usr/bin/env bash
# Daily portfolio monitor run.
#
#   scripts/daily.sh [positions.xlsx] [--today YYYY-MM-DD]
#
# Picks the newest workbook in data/ when no file is given, reconciles it,
# runs the full rule set, writes reports/<date>.txt and reports/latest.txt,
# and appends the snapshot to state/history.json. Exit code is 1 when
# reconciliation fails so a scheduler can flag it.
set -euo pipefail
cd "$(dirname "$0")/.."

FILE="${1:-}"
shift || true
if [[ -z "$FILE" ]]; then
  FILE="$(ls -t data/*.xlsx 2>/dev/null | head -1 || true)"
fi
if [[ -z "$FILE" || ! -f "$FILE" ]]; then
  echo "No positions export found. Put the workbook in data/ or pass its path." >&2
  exit 2
fi

[[ -f config.json ]] || cp config.example.json config.json
mkdir -p reports state
STAMP="$(date +%Y-%m-%d)"

python3 -m monitor.monitor check "$FILE" "$@" > "reports/check-$STAMP.json" || CHECK_RC=$?
python3 -m monitor.monitor run "$FILE" "$@" --out "reports/$STAMP.txt"
cp "reports/$STAMP.txt" reports/latest.txt
exit "${CHECK_RC:-0}"
