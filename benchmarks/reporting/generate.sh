#!/bin/bash

# Regenerate every file in benchmarks/reports from the saved results, then check
# them: published data set and summary, HTML report, pull request draft, SVG and
# PNG figures. Nothing is measured; campaign/run.py and campaign/validate.sh
# produce the inputs.

set -eu -o pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
REPORTS=$(cd "$HERE/../reports" && pwd)
# The HTML checks save screenshots; keep them only when CHECK_DIR is given.
if [ -z "${CHECK_DIR:-}" ]; then
    CHECK_DIR=$(mktemp -d)
    trap 'rm -rf "$CHECK_DIR"' EXIT
fi
export PYTHONDONTWRITEBYTECODE=1
# The campaign whose raw results are analyzed; reproduce.sh sets it.
WORK=${WORK:-$(cd "$HERE/../artifacts/campaign" && pwd)}

START=${REPRODUCE_START:-$(date +%s)}

log() {
    local seconds=$(($(date +%s) - START))
    printf '[%d:%02d]   %s\n' $((seconds / 60)) $((seconds % 60)) "$*"
}
# Print failures and other messages, and count the passing checks.
summarize() {
    awk '/^PASS/ { n++; next } { print "  " $0 } END { print "  " n + 0 " checks passed" }'
}

log "Data set and summary (analyze.py)"
python3 "$HERE/analyze.py" --raw "$WORK/raw" --work "$WORK" | sed 's/^/  /'
log "HTML report, pull request draft and SVG figures (report.py)"
python3 "$HERE/report.py" | sed 's/^/  /'
log "PNG figures (render.py)"
python3 "$HERE/render.py" | sed 's/^/  /'
log "Consistency checks (check_consistency.py)"
python3 "$HERE/check_consistency.py" | summarize
log "HTML report checks in headless Chrome (check_html.mjs)"
node "$HERE/check_html.mjs" "$REPORTS/performance-comparison.html" "$CHECK_DIR" | summarize
