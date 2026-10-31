#!/bin/bash

# Build isolated Before/After source snapshots, identical Release libraries and
# the benchmark executables for campaign 3. Existing outputs are never
# overwritten: choose another WORK directory to rebuild from scratch.

set -eu -o pipefail

REPO=$(git -C "$(dirname "$0")" rev-parse --show-toplevel)
WORK=${WORK:-$REPO/benchmarks/artifacts/campaign}
BEFORE_SHA=${BEFORE_SHA:-88ad5f46d9c884fc4a8533374b457cb9e3ee121f}
# The published executables were built from 8317173, an earlier commit with the
# same source tree as fd08b22; both give byte-identical libraries and executables.
AFTER_SHA=${AFTER_SHA:-fd08b229052ae34e9f2c3c3df4f64ed9d43f4347}
CC=${CC:-gcc}
JOBS=${JOBS:-8}
BENCH_SOURCE=$REPO/benchmarks/harness/bench.c
# Equivalent settings for both revisions: CMake's GCC Release flags.
HARNESS_FLAGS=(-std=c11 -O3 -DNDEBUG -Wall -Wextra -Werror -pthread)
ALLOC_FLAGS=(-DBENCH_COUNT_ALLOCATIONS
    -Wl,--wrap=malloc,--wrap=calloc,--wrap=realloc,--wrap=free)

# Separate output directories keep earlier campaign binaries untouched.
BIN=${BIN:-$WORK/bin}
PROVENANCE=${PROVENANCE:-$WORK/provenance}
mkdir -p "$WORK"/{src,build,logs} "$BIN" "$PROVENANCE"

snapshot() {
    local name=$1 sha=$2
    local dest=$WORK/src/$name
    git -C "$REPO" cat-file -e "$sha^{commit}"
    if [ -e "$dest" ]; then
        # Reuse an existing snapshot only if it is byte-identical.
        if [ "$(cat "$dest/.snapshot-commit")" != "$sha" ] ||
            ! git -C "$REPO" archive --format=tar --prefix="$name/" "$sha" |
            cmp -s - "$WORK/src/$name.tar"; then
            echo "Refusing to reuse a different snapshot in $dest" >&2
            exit 1
        fi
        return
    fi
    git -C "$REPO" archive --format=tar --prefix="$name/" "$sha" \
        >"$WORK/src/$name.tar"
    tar -x -C "$WORK/src" -f "$WORK/src/$name.tar"
    # git archive omits submodules. Copy the exact pinned commits.
    for module in t/libtap t/maxmind-db; do
        local pinned actual
        pinned=$(git -C "$REPO" ls-tree "$sha" "$module" | awk '{print $3}')
        actual=$(git -C "$REPO/$module" rev-parse HEAD)
        if [ "$pinned" != "$actual" ]; then
            echo "$module is at $actual, but $sha pins $pinned" >&2
            exit 1
        fi
        git -C "$REPO/$module" archive --format=tar \
            --prefix="$name/$module/" "$pinned" | tar -x -C "$WORK/src"
    done
    git -C "$REPO" rev-parse "$sha^{tree}" >"$dest/.snapshot-tree"
    echo "$sha" >"$dest/.snapshot-commit"
}

build_library() {
    local name=$1
    cmake -S "$WORK/src/$name" -B "$WORK/build/$name-release" -G Ninja \
        -DCMAKE_BUILD_TYPE=Release \
        -DCMAKE_C_COMPILER="$CC" \
        -DBUILD_SHARED_LIBS=OFF \
        -DBUILD_TESTING=ON \
        -DMAXMINDDB_BUILD_BINARIES=ON \
        -DCMAKE_EXPORT_COMPILE_COMMANDS=ON \
        >"$WORK/logs/configure-$name-release.log" 2>&1
    cmake --build "$WORK/build/$name-release" -j "$JOBS" \
        >"$WORK/logs/build-$name-release.log" 2>&1
}

build_bench() {
    local name=$1 suffix=$2
    shift 2
    local output=$BIN/bench-$name$suffix
    local command=("$CC" "${HARNESS_FLAGS[@]}" "$@"
        -I"$WORK/src/$name/include"
        -I"$WORK/build/$name-release/generated"
        "$BENCH_SOURCE" "$WORK/build/$name-release/libmaxminddb.a"
        -lcrypto -o "$output")
    printf '%q ' "${command[@]}" >"$PROVENANCE/compile-bench-$name$suffix.txt"
    echo >>"$PROVENANCE/compile-bench-$name$suffix.txt"
    "${command[@]}"
}

snapshot before "$BEFORE_SHA"
snapshot after "$AFTER_SHA"

# Record the main working tree at build time. Only .gitignore/.precious.toml
# were modified there; they do not affect the library and are not applied.
git -C "$REPO" rev-parse HEAD >"$PROVENANCE/head.txt"
git -C "$REPO" status --porcelain=v1 >"$PROVENANCE/working-tree-status.txt"
git -C "$REPO" diff HEAD >"$PROVENANCE/working-tree.diff"
git -C "$REPO" log --format='%H %ad %s' --date=iso \
    "$BEFORE_SHA..$AFTER_SHA" >"$PROVENANCE/commits.txt"
git -C "$REPO" diff --stat "$BEFORE_SHA" "$AFTER_SHA" \
    >"$PROVENANCE/diffstat.txt"
git -C "$REPO" diff "$BEFORE_SHA" "$AFTER_SHA" \
    >"$PROVENANCE/before-after.diff"

for name in before after; do
    build_library "$name"
    build_bench "$name" ""
    build_bench "$name" "-alloc" "${ALLOC_FLAGS[@]}"
    cp "$WORK/build/$name-release/compile_commands.json" \
        "$PROVENANCE/compile_commands-$name.json"
done

# Each executable must contain its own static library, never a system copy.
# Tool outputs are stored before they are searched, so set -e stops on a tool
# failure; `tool | grep -q` would instead fail at random under pipefail, when
# grep exits on an early match while the tool is still writing (SIGPIPE).
for binary in "$BIN"/bench-*; do
    libraries=$(ldd "$binary")
    if grep -q maxminddb <<<"$libraries"; then
        echo "$binary links a shared libmaxminddb" >&2
        exit 1
    fi
done
symbols=$(nm "$BIN/bench-after")
if ! grep -q ' T MMDB_create_lookup_context$' <<<"$symbols"; then
    echo "After executable lacks the After library" >&2
    exit 1
fi
symbols=$(nm "$BIN/bench-before")
if grep -q 'MMDB_create_lookup_context' <<<"$symbols"; then
    echo "Before executable contains After symbols" >&2
    exit 1
fi

{
    for binary in "$BIN"/bench-*; do
        echo "== $binary"
        ldd "$binary"
    done
} >"$PROVENANCE/ldd.txt"
sha256sum "$BIN"/bench-* "$WORK"/build/*-release/libmaxminddb.a \
    "$BENCH_SOURCE" "$WORK"/src/*.tar >"$PROVENANCE/SHA256SUMS"
compiler=$("$CC" --version)
generator=$(cmake --version)
printf '%s\n' "${compiler%%$'\n'*}" "${generator%%$'\n'*}" >"$PROVENANCE/compiler.txt"
echo "Build complete: $WORK"
