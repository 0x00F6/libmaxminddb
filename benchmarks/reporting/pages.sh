#!/bin/bash

# Build the GitHub Pages site from campaigns measured on several architectures.
# Everything but the landing page index.html goes under the After commit's short
# hash: <commit>/<architecture>/ holds the HTML report, its figures
# (performance-comparison-assets/) and data (performance-comparison-data/), and
# <commit>/performance-comparison-<branch>.html switches between the
# architectures on one page. Every report is generated and checked by
# generate.sh, so this needs Chrome and Node.js like generate.sh. BRANCH
# defaults to GitHub's GITHUB_REF_NAME, then to the checked-out branch.
#
# Usage: benchmarks/reporting/pages.sh CAMPAIGNS_DIRECTORY SITE_DIRECTORY
# CAMPAIGNS_DIRECTORY holds one campaign per architecture, named
# campaign-<architecture> (the WORK directory of benchmarks/reproduce.sh).

set -eu -o pipefail

if [ $# -ne 2 ]; then
    echo "Usage: $0 CAMPAIGNS_DIRECTORY SITE_DIRECTORY" >&2
    exit 2
fi
HERE=$(cd "$(dirname "$0")" && pwd)
CAMPAIGNS=$1
SITE=$2
export PYTHONDONTWRITEBYTECODE=1
# The After commit of the comparison, as analyze.py records it.
COMMIT=$(python3 -c 'import sys; sys.path.insert(0, sys.argv[1]); import analyze; print(analyze.AFTER_SHA[:7])' "$HERE")
mkdir -p "$SITE/$COMMIT"

found=0
for campaign in "$CAMPAIGNS"/campaign-*/; do
    [ -d "$campaign/raw" ] || continue
    architecture=$(basename "$campaign")
    architecture=${architecture#campaign-}
    echo "== $architecture"
    WORK=${campaign%/} REPORTS=$SITE/$COMMIT/$architecture "$HERE/generate.sh"
    found=$((found + 1))
done
if [ "$found" = 0 ]; then
    echo "No campaign-<architecture> directory with results in $CAMPAIGNS" >&2
    exit 1
fi
if [ "$found" -gt 1 ]; then
    BRANCH=${BRANCH:-${GITHUB_REF_NAME:-$(git -C "$HERE" rev-parse --abbrev-ref HEAD)}}
    # benchmarks/fd08b22 gives performance-comparison-benchmarks-fd08b22.html.
    MERGED=$SITE/$COMMIT/performance-comparison-${BRANCH//[^A-Za-z0-9._-]/-}.html
    echo "== merged"
    python3 "$HERE/report.py" --merge "$SITE/$COMMIT" --output "$MERGED"
    CHECK_DIR=$(mktemp -d)
    trap 'rm -rf "$CHECK_DIR"' EXIT
    first=$(find "$SITE/$COMMIT" -mindepth 3 -maxdepth 3 -path '*/performance-comparison-data/summary.json' | sort | head -1)
    for preferred in x86_64 aarch64; do
        if [ -f "$SITE/$COMMIT/$preferred/performance-comparison-data/summary.json" ]; then
            first=$SITE/$COMMIT/$preferred/performance-comparison-data/summary.json
            break
        fi
    done
    node "$HERE/check_html.mjs" "$MERGED" "$CHECK_DIR" "$first" |
        awk '/^PASS/ { n++; next } { print "  " $0 } END { print "  " n + 0 " checks passed" }'
fi
python3 "$HERE/pages.py" "$SITE"
