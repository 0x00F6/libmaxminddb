#!/bin/bash

# Run the project's test suites against the campaign snapshots and record the
# results for the report. Run after the measurements: it is CPU intensive.

set -eu -o pipefail

REPO=$(git -C "$(dirname "$0")" rev-parse --show-toplevel)
WORK=${WORK:-$REPO/benchmarks/artifacts/campaign}
DATA=${DATA:-$REPO/benchmarks/reports/performance-comparison-data}
JOBS=${JOBS:-8}
LOGS=$WORK/logs
mkdir -p "$LOGS" "$DATA"
START=${REPRODUCE_START:-$(date +%s)}

log() {
    local seconds=$(($(date +%s) - START))
    printf '[%d:%02d]   %s\n' $((seconds / 60)) $((seconds % 60)) "$*"
}

ctest_suite() {
    local name=$1 build=$2
    shift 2
    if env "$@" ctest --test-dir "$build" --output-on-failure -j "$JOBS" \
        >"$LOGS/ctest-$name.log" 2>&1; then
        log "$name: pass"
    else
        log "$name: FAIL (see $LOGS/ctest-$name.log)"
    fi
}

# Release builds produced by build.sh, including their test programs.
ctest_suite before-release "$WORK/build/before-release"
ctest_suite after-release "$WORK/build/after-release"

# AddressSanitizer and UndefinedBehaviorSanitizer build of After.
log "after-sanitize: building After with ASan and UBSan"
cmake -S "$WORK/src/after" -B "$WORK/build/after-sanitize" -G Ninja \
    -DCMAKE_BUILD_TYPE=Debug -DCMAKE_C_COMPILER=clang \
    -DCMAKE_C_FLAGS='-fsanitize=address,undefined -fno-sanitize-recover=all -fno-omit-frame-pointer' \
    -DBUILD_TESTING=ON >"$LOGS/configure-after-sanitize.log" 2>&1
cmake --build "$WORK/build/after-sanitize" -j "$JOBS" >"$LOGS/build-after-sanitize.log" 2>&1
ctest_suite after-sanitize "$WORK/build/after-sanitize" \
    ASAN_OPTIONS=detect_leaks=1:halt_on_error=1 UBSAN_OPTIONS=halt_on_error=1

# Autotools build and make check of After in a disposable copy.
log "after-autotools: bootstrap, configure, make check"
rm -rf "$WORK/build/after-autotools"
cp -a "$WORK/src/after" "$WORK/build/after-autotools"
(
    cd "$WORK/build/after-autotools"
    ./bootstrap >"$LOGS/autotools-bootstrap.log" 2>&1
    ./configure >"$LOGS/autotools-configure.log" 2>&1
    make -j "$JOBS" >"$LOGS/autotools-make.log" 2>&1
    if make -j "$JOBS" check >"$LOGS/autotools-check.log" 2>&1; then
        log "after-autotools: pass"
    else
        log "after-autotools: FAIL (see $LOGS/autotools-check.log)"
    fi
)

python3 - "$LOGS" "$WORK" "$DATA/tests.json" <<'EOF'
import json, re, sys
from pathlib import Path
logs, work, output = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
result = {}
for name in ('before-release', 'after-release', 'after-sanitize'):
    text = (logs / f'ctest-{name}.log').read_text()
    m = re.search(r'(\d+)% tests passed, (\d+) tests failed out of (\d+)', text)
    result[name] = {'tool': 'ctest', 'total': int(m.group(3)), 'failed': int(m.group(2)),
                    'passed': int(m.group(3)) - int(m.group(2)), 'log': f'logs/ctest-{name}.log'}
check = (logs / 'autotools-check.log').read_text()
# The serial Automake driver prints "All N tests passed" or "M of N tests failed".
passed = re.search(r'All (\d+) tests passed', check)
failed = re.search(r'(\d+) of (\d+) tests failed', check)
total = int(passed.group(1)) if passed else int(failed.group(2))
# Perl scripts that lack optional modules print "1..0 # skip" and still PASS.
self_skipped = re.findall(r'^1\.\.0 # skip.*?^PASS: (\S+)', check, re.M | re.S)
result['after-autotools'] = {'tool': 'make check', 'total': total,
                             'failed': int(failed.group(1)) if failed else 0,
                             'passed': total - (int(failed.group(1)) if failed else 0) - len(self_skipped),
                             'skipped': len(self_skipped), 'skipped_tests': self_skipped,
                             'log': 'logs/autotools-check.log'}
output.write_text(json.dumps(result, indent=1) + '\n')
print('  tests.json: ' + ', '.join(f'{k} {v["passed"]}/{v["total"]}' for k, v in result.items()))
EOF
