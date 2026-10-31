# Performance comparison — data

Every report, table and image is generated from `summary.json`; the other files
keep the per-run results it was computed from. The compared commits, the commit
the executables were built from and the databases are in `summary.json`
(`before`, `after`, `databases`); the machine is in `environment.json`.
Regenerate everything without measuring:

```sh
benchmarks/reporting/generate.sh
```

| File                                            | Contents                                                                                                                                                                                                               |
| ----------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `summary.json`                                  | Single source of truth: per-configuration statistics, comparisons, scaling, latency percentiles, memory, startup, allocations, attribution, workloads, databases, environment, validation and headline geometric means |
| `runs.json`                                     | Every throughput and latency repetition ({measured_runs} runs) with scalar results: elapsed time, throughput, hits, checksum, CPU time, context switches, APERF/MPERF, RSS stages                                      |
| `runs-throughput.csv`, `runs-latency.csv`       | The same per-run scalars as CSV, one file per campaign                                                                                                                                                                 |
| `latency-histograms.json`                       | Exact integer-nanosecond histograms of the sampled latencies of every latency run (workers merged)                                                                                                                     |
| `workers.json`                                  | Per-worker CPU, partition, elapsed time, CPU time, context switches, APERF/MPERF and checksum for every run                                                                                                            |
| `attribution-runs.json`, `runs-attribution.csv` | Three-way Before / After with `MMDB_NO_PREPARE` / After campaign                                                                                                                                                       |
| `memory-runs.json`, `memory-runs.csv`           | Fresh-process RSS, heap and mapping residency at each stage                                                                                                                                                            |
| `startup-runs.json`, `startup-runs.csv`         | Fresh-process `MMDB_open()` and preparation timings, warm and cold page cache                                                                                                                                          |
| `allocations-runs.json`, `allocations-runs.csv` | Allocation counts and bytes from the instrumented binaries                                                                                                                                                             |
| `verify-runs.json`, `verify-runs.csv`           | Ordered per-query result digests used for the differential check                                                                                                                                                       |
| `workloads.json`                                | Corpus SHA-256, hit/miss counts, populated-network counts and prefix-length histograms per workload                                                                                                                    |
| `environment.json`                              | CPU, topology, frequency policy, memory, OS, toolchain, build commands, binary/library/database hashes                                                                                                                 |
| `tests.json`                                    | Test-suite results from `benchmarks/campaign/validate.sh`                                                                                                                                                              |
| `host-activity.jsonl`                           | Load average, system CPU use and top background processes sampled during the measurements                                                                                                                              |
| `provenance/`                                   | HEAD, working-tree state and diff, commit list, diffstat, compile commands, `ldd` output, binary hashes, the harness source and runner, and the machine description at the start and end of the campaign               |
| `SHA256SUMS`                                    | Checksums of every file in this directory and `provenance/`                                                                                                                                                            |

Units: throughput in million lookups per second (M/s), latency in nanoseconds,
memory in KiB in the raw rows and MiB in `summary.json`, time in nanoseconds in
raw rows and milliseconds in `summary.json`.
