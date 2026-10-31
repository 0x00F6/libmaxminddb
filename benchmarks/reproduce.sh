#!/bin/bash

# Reproduce the comparison in one command: build both revisions, create the
# databases, run the campaigns and the test suites, then regenerate
# benchmarks/reports. Every step prints its number and the time elapsed since
# the start.
#
# Usage: benchmarks/reproduce.sh /path/to/GeoLite2-City.mmdb
#
# The default protocol stays under five minutes on the measurement machine
# (about four): every query count is 10M, the timed campaigns cover random
# GeoLite2-City workloads with 5 repetitions, and the untimed ones (differential
# check, allocations, memory) run several processes at once. FULL=1 runs the
# complete matrix of the earlier campaigns instead: both databases, both
# profiles, 10 repetitions, about an hour.
#
# Everything is written to WORK (default benchmarks/artifacts/campaign). A
# campaign already there is moved, with a copy of benchmarks/reports, to
# benchmarks/artifacts/archive/ first; the reports are then replaced.

set -eu -o pipefail

if [ $# -ne 1 ]; then
    echo "Usage: $0 /path/to/GeoLite2-City.mmdb" >&2
    exit 2
fi
GEOLITE=$1
if [ ! -f "$GEOLITE" ]; then
    echo "$GEOLITE: no such file" >&2
    exit 2
fi
HERE=$(cd "$(dirname "$0")" && pwd)
# One run at a time: a second one would archive the campaign under way.
mkdir -p "$HERE/artifacts"
exec 9>"$HERE/artifacts/.reproduce.lock"
if ! flock -n 9; then
    echo "Another benchmarks/reproduce.sh is already running; wait for it to finish." >&2
    exit 1
fi
export WORK=${WORK:-$HERE/artifacts/campaign}
export PYTHONDONTWRITEBYTECODE=1
# run.py, validate.sh and generate.sh print the time since this start too.
REPRODUCE_START=$(date +%s)
export REPRODUCE_START
BUDGET_SECONDS=300

elapsed() {
    local seconds=$(($(date +%s) - REPRODUCE_START))
    printf '%d:%02d' $((seconds / 60)) $((seconds % 60))
}
STEP=0
STEPS=9
step() {
    STEP=$((STEP + 1))
    printf '\n[%s] Step %d/%d: %s\n' "$(elapsed)" "$STEP" "$STEPS" "$*"
}
log() {
    printf '[%s]   %s\n' "$(elapsed)" "$*"
}
trap 'echo "[$(elapsed)] FAILED during step $STEP/$STEPS" >&2' ERR

if [ "${FULL:-0}" = 1 ]; then
    PROTOCOL='full matrix, about an hour'
    UNTIMED=()
    TIMED=()
    LATENCY=()
else
    PROTOCOL='quick protocol, under five minutes'
    UNTIMED=(--threads 1 --memory-iterations 3)
    TIMED=(--databases city --profiles random --reps 5)
    LATENCY=(--operations lookup)
fi
RUN=(python3 "$HERE/campaign/run.py" --work "$WORK" --output "$WORK/raw")
log "Reproducing the Before/After comparison ($PROTOCOL) in $WORK"

PREVIOUS_GEOLITE_SHA256=
if [ -e "$WORK/raw" ] || [ -e "$WORK/bin" ]; then
    # Keep the previous campaign and the reports generated from it.
    if [ -f "$WORK/raw/environment-start.json" ]; then
        PREVIOUS_GEOLITE_SHA256=$(python3 -c 'import json, sys
files = json.load(open(sys.argv[1]))["files"]
print((files.get("data/GeoLite2-City.mmdb") or {}).get("sha256", ""))' "$WORK/raw/environment-start.json")
    fi
    ARCHIVE=$HERE/artifacts/archive/$(basename "$WORK")-$(date -u +%Y%m%dT%H%M%SZ)
    mkdir -p "$ARCHIVE"
    mv "$WORK" "$ARCHIVE/campaign"
    cp -a "$HERE/reports" "$ARCHIVE/reports"
    log "Previous campaign and reports moved to $ARCHIVE"
fi

step "Build Before, After and the benchmark harness"
"$HERE/harness/build.sh" | sed "s/^/  /"

step "Databases"
mkdir -p "$WORK/data" "$WORK/raw"
python3 "$HERE/harness/generate_synthetic_database.py" "$WORK/data/synthetic.mmdb" >/dev/null
log "Synthetic fixture generated"
cp "$GEOLITE" "$WORK/data/GeoLite2-City.mmdb"
log "GeoLite2-City copied from $GEOLITE"
if [ -n "$PREVIOUS_GEOLITE_SHA256" ] &&
    [ "$(sha256sum <"$WORK/data/GeoLite2-City.mmdb" | cut -d' ' -f1)" != "$PREVIOUS_GEOLITE_SHA256" ]; then
    echo "warning: $GEOLITE differs from the previous campaign's GeoLite2-City;" \
        "the results are not directly comparable" >&2
fi
# Write the new files back now: cold opens can only evict clean cached pages.
sync "$WORK/data/synthetic.mmdb" "$WORK/data/GeoLite2-City.mmdb"
python3 "$HERE/campaign/environment.py" "$WORK" >"$WORK/raw/environment-start.json"

# Nothing is timed in these three, so they run several processes at once.
step "Differential check, allocation counts and memory (untimed, in parallel)"
"${RUN[@]}" verify allocations memory "${UNTIMED[@]}"

# The timed campaigns run one process or one pair of processes at a time.
step "Startup: database opening and index preparation (timed)"
"${RUN[@]}" startup

step "Throughput (timed)"
"${RUN[@]}" throughput "${TIMED[@]}"

step "Latency (timed)"
"${RUN[@]}" latency "${TIMED[@]}" "${LATENCY[@]}"

# The harness built above has --no-prepare, so the attribution campaign uses the
# same executables.
step "Attribution: packed traversal alone vs automatic preparation (timed)"
"${RUN[@]}" attribution "${TIMED[@]}" --databases city --threads 1 8
python3 "$HERE/campaign/environment.py" "$WORK" >"$WORK/raw/environment-end.json"

step "Test suites"
"$HERE/campaign/validate.sh"
# Only the test suites need the extracted snapshots and build trees;
# harness/build.sh recreates them.
rm -rf "$WORK/build" "$WORK/src/before" "$WORK/src/after"

step "Reports"
"$HERE/reporting/generate.sh"

TOTAL=$(($(date +%s) - REPRODUCE_START))
printf '\n[%s] Done in %s. Report: %s\n' "$(elapsed)" "$(elapsed)" "$HERE/reports/performance-comparison.html"
if [ "${FULL:-0}" != 1 ] && [ "$TOTAL" -gt "$BUDGET_SECONDS" ]; then
    echo "warning: the run took ${TOTAL} s, more than the ${BUDGET_SECONDS} s budget" >&2
fi
