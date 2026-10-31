# Before/After comparison

Optional Linux-only tooling that compares upstream `main` at
`88ad5f46d9c884fc4a8533374b457cb9e3ee121f` (Before) with
`fd08b229052ae34e9f2c3c3df4f64ed9d43f4347` (After) on the machine that runs it.
Each campaign records the commit its executables were built from and the
databases it used in `summary.json`.
Nothing here changes the library build or adds runtime dependencies. The
harness uses C11, POSIX threads, Linux `/proc` interfaces and OpenSSL libcrypto
for untimed SHA-256 digests. Python 3.11 or newer drives the campaigns and
generates the reports; PNG rendering needs Chrome or Chromium.

## Layout

| Path | Purpose |
| --- | --- |
| `harness/bench.c` | the benchmark driver linked against each library |
| `harness/build.sh` | `git archive` snapshots of both commits, pinned submodules, CMake Release static libraries, timed and allocation binaries, linkage checks |
| `harness/generate_synthetic_database.py` | deterministic synthetic fixture (`synthetic.mmdb`) used as the second database |
| `campaign/run.py` | paired throughput and latency campaigns plus fresh-process memory, startup, allocation and differential campaigns |
| `campaign/environment.py` | CPU, topology, frequency policy, memory, OS, toolchain and file hashes as JSON |
| `campaign/validate.sh` | CMake, sanitizer and Autotools test suites of the snapshots; writes `tests.json` |
| `reporting/analyze.py` | raw JSON Lines to `summary.json`, per-run JSON/CSV and metadata |
| `reporting/report.py` | HTML report, pull request draft, SVG exports and image manifest from `summary.json` |
| `reporting/charts.py` | dependency-free SVG chart rendering shared by the HTML report and the exports |
| `reporting/render.py` | PNG exports (2x) of the SVG charts through headless Chrome |
| `reporting/check_consistency.py` | cross-checks numbers, labels, commits and PNG sizes across the HTML report, PR draft, exports and `summary.json` |
| `reproduce.sh` | the whole comparison in one command: build, databases, campaigns, test suites and reports |
| `reporting/generate.sh` | regenerates and checks every file in `reports/` from the saved results in one command |
| `reporting/check_html.mjs` | drives the HTML report in headless Chrome (filters, tooltips, focus, sorting, console errors) and saves screenshots |
| `artifacts/archive/` | earlier campaigns and the reports generated from them, set aside by `reproduce.sh` |
| `artifacts/campaign/` | source snapshots (`src/*.tar`), measured executables (`bin/`, `bin-attribution/`), databases (`data/`), raw results (`raw/`), logs and provenance |
| `reports/` | the HTML report, its figures (`performance-comparison-assets/`), the published data (`performance-comparison-data/`) and the pull request draft |

The extracted source snapshots and CMake build trees are not kept: `harness/build.sh`
recreates them from the archived commits, and `campaign/validate.sh` needs them.
Set `WORK` to another directory for both scripts to leave the measured
executables and provenance untouched.

## What is measured

- `lookup`: `MMDB_lookup_sockaddr()` on `sockaddr` structures built before
  timing. No text parsing happens in the timed loop.
- `decode`: `lookup` followed by `MMDB_get_value()` on hits: `country` →
  `iso_code` on GeoLite2-City and `id` on the synthetic fixture. Records without
  the field count as absent fields, identically in both versions.
- `random`: SplitMix64 addresses (seed `0x6a09e667f3bcc909`) over the whole IPv4
  or IPv6 space.
- `hits`: every reachable data leaf is enumerated with `MMDB_read_node()` and
  uniformly reservoir-sampled (2^20 networks); host bits are randomized. IPv6
  enumeration skips the IPv4 subtree and its aliases (`::ffff:0:0/96`,
  `2002::/16` and others), so IPv6 hits are native IPv6 networks. Selection is
  uniform over networks, not over address space.

Every campaign runs 10M queries (`run.py --sizes`). The query count is the
total work, split into contiguous partitions: 10,000,000 lookups for one
worker, 2,500,000 for each of four and 1,250,000 for each of eight. Campaigns
recorded before 1M-query runs were dropped still hold them in `raw/`;
`analyze.py` leaves them out of the published data, except their 1M allocation
counts when no 10M ones exist. Workers are pinned to the first SMT sibling of
distinct physical cores in ACPI CPPC rank order, so 1, 4 and 8 workers never
share a core. The library has no internal parallelism.

## Protocol

- Throughput: one prepared process per version and configuration. The runner
  triggers exactly one repetition at a time; even pairs run Before first, odd
  pairs After first. Each repetition creates the workers, warms up with the
  first 1,000,000 queries of the configuration, waits on a spin start gate,
  then runs the full partition. Elapsed time goes from gate release to the last
  worker's finish. Checksums, hit and decode
  counters are thread-local; there are no per-query timers or shared counters.
- Latency: a separate campaign with the same structure that times every 16th
  operation with two `CLOCK_MONOTONIC_RAW` reads. Exact integer-nanosecond
  histograms of all workers are merged before nearest-rank percentiles
  `floor((n - 1) * p)` are computed; reports pool all repetitions. Timer
  overhead is measured and included, not subtracted. The clock advances in steps
  of about 10 ns on the measurement CPU.
- Memory: fresh process per measurement (3 per configuration with
  `reproduce.sh`, 5 by default in `run.py`) with the same preparation and
  warm-up. Nothing is timed, so several processes run at once (`--jobs`), each
  pinned to its own core. RSS stages come from `/proc/self/status`; the lookup
  peak resets `VmHWM` through `/proc/self/clear_refs` after warm-up; heap is
  `mallinfo2()` in-use bytes; database residency comes from `/proc/self/smaps`.
- Startup: fresh pinned processes, 30 warm and 10 cold opens per variant
  (`MMDB_open()` with default flags, with `MMDB_NO_PREPARE`, and
  `MMDB_create_lookup_context()` alone). Cold opens evict the file with
  `posix_fadvise(POSIX_FADV_DONTNEED)` and verify zero resident pages.
- Allocations: separate binaries linked with
  `-Wl,--wrap=malloc,--wrap=calloc,--wrap=realloc,--wrap=free` count
  allocations during `MMDB_open()` and the measured lookup phase only. They are
  never used for timing and run several at once (`--jobs`), with one measured
  repetition each because the counts are deterministic.
- Differential: an ordered SHA-256 over every query's found flag, prefix length,
  entry offset, errors and decoded value must match between versions for every
  corpus. Several processes run at once (`--jobs`).
- Throughput, latency, attribution and startup are timed: they run one process,
  or one pair of processes, at a time.

Every repetition checks its completed count and zero lookup or decode errors.
The analysis checks identical checksums across versions and repetitions.

## Reproduction

One command builds both revisions, creates the databases, runs every campaign
(including the attribution campaign below), runs the test suites and
regenerates `reports/`. It takes under four minutes on the measurement machine
(3:43 on 2026-10-31) and stays under five:

```sh
benchmarks/reproduce.sh /path/to/GeoLite2-City.mmdb
```

To fit that budget the timed campaigns are restricted: throughput covers
GeoLite2-City with random addresses, lookup and lookup + decode, IPv4 and IPv6,
1, 4 and 8 threads; latency the lookups of that set; attribution its lookups on
1 and 8 threads; each with 5 repetitions. The differential check, allocation
counts, memory and startup still cover both databases and both workload
profiles. `FULL=1 benchmarks/reproduce.sh …` runs the complete matrix of the
earlier campaigns instead (both databases and profiles, 10 repetitions, about
an hour).

The script prints a numbered step (`Step 3/9: …`) and every progress line with
the time elapsed since the start, for example:

```
[0:50] Step 5/9: Throughput (timed)
[0:50]   throughput 1/12 city random lookup IPv4 10000000 x1
[1:07]   throughput 2/12 city random lookup IPv4 10000000 x4
```

It reports the total at the end and warns when the quick protocol exceeds five
minutes. When a step fails it prints which one. Only one run at a time is
allowed (lock file `artifacts/.reproduce.lock`), since a second one would
archive the campaign under way.

Everything goes to `WORK` (default `benchmarks/artifacts/campaign`). A campaign
already there is first moved, with a copy of `reports/`, to
`artifacts/archive/<name>-<UTC time>/`, and the script warns if the
GeoLite2-City file differs from the one that campaign used. The reports in
`reports/` are then replaced by those of the new campaign.

GeoLite2-City cannot be redistributed; the reports give the build date and
SHA-256 of the file each campaign used. The synthetic fixture regenerates byte-identically (SHA-256
`4adee8dcd7ada2dae2e8516a02f8093075ac4fabfc4b7a2e81dbf00effb18767`).

`run.py` refuses to append to existing results unless `--resume` is given, in
which case complete configurations are kept and incomplete ones are moved to
`discarded-incomplete.jsonl` and rerun. Small validation runs can restrict any
dimension, for example `--sizes 100000 --reps 2 --output /tmp/pilot`.

`reporting/generate.sh` only reads saved results, so every report, table and
image can be regenerated without measuring again.

The as-launched copy of `run.py` used for the published data is kept in
`benchmarks/artifacts/campaign/provenance/run.py.as-launched`. It stored the
harness's database path instead of the short database name in startup and
differential rows; `analyze.py` maps those paths back to the short names.

## Attribution campaign

The main matrix measures the complete change set. A supplementary three-way
campaign separates the two traversal changes: Before, After opened with
`MMDB_NO_PREPARE` (only the specialized packed traversal) and After (automatic
radix-16 preparation). It needs the `--no-prepare` harness option. For the
published data that option was added after the main campaign, so the
attribution campaign used its own executables (`bin-attribution/`) and
provenance (`provenance-attribution/`). `reproduce.sh` builds the current
harness once and runs both campaigns with the same executables.

The main campaign's harness source is preserved as
`benchmarks/artifacts/campaign/provenance/bench.c.main-campaign` (SHA-256
`446a754c…`). Without `--no-prepare` the current `bench.c` executes the same
measured code.
