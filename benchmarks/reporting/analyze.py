#!/usr/bin/env python3
"""Turn the campaign's raw JSON Lines into the published data set and summary.

Every report, table, chart and the pull request description are generated from
summary.json written here, so all deliverables share one set of numbers.

Formulas (applied consistently everywhere):
  throughput speedup    = after / before                (medians)
  throughput change %   = (after / before - 1) * 100
  latency reduction %   = (1 - after / before) * 100
  memory change %       = (after / before - 1) * 100
  thread scaling        = throughput(N) / throughput(1), same version
  parallel efficiency % = thread_scaling / N * 100
A zero or missing denominator yields null, never infinity.
"""
import argparse
import csv
import datetime
import hashlib
import json
import math
import random
import shutil
import statistics
import subprocess
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RAW = ROOT / 'benchmarks/artifacts/campaign/raw'
DEFAULT_WORK = ROOT / 'benchmarks/artifacts/campaign'
DEFAULT_DATA = ROOT / 'benchmarks/reports/performance-comparison-data'
BOOTSTRAP = 10_000
BOOTSTRAP_SEED = 0xC0FFEE3
VERSIONS = ('before', 'after')
BEFORE_SHA = '88ad5f46d9c884fc4a8533374b457cb9e3ee121f'
# After is the last of the three pull request commits. The commit the executables were built from is read from the
# After source snapshot: the published ones come from 8317173, an earlier commit with the identical source tree.
AFTER_COMMITS = ('3ea223aa354d4de3bd64078e0581a74845dcde41', '5e3614964ccf54bced801142c31fa78ec6da14ad',
                 'fd08b229052ae34e9f2c3c3df4f64ed9d43f4347')
AFTER_SHA = AFTER_COMMITS[-1]
AFTER_TREE = 'fc0ac2c119311cf08008b8c3117192ebb7a88a72'
# Workload size of the published results. Older campaigns also ran 1M-query
# configurations; their rows are left out.
SIZES = (10_000_000,)


DATABASE_FILES = {'GeoLite2-City.mmdb': 'city', 'synthetic.mmdb': 'synthetic'}


def jsonl(path):
    if not path.exists():
        return []
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    # The as-launched runner let the harness's database path replace the short
    # name in startup and verify rows; map the path back to the short name.
    for row in rows:
        name = row.get('database')
        if isinstance(name, str) and name.endswith('.mmdb'):
            row['database_path'] = name
            row['database'] = DATABASE_FILES[Path(name).name]
    return rows


def published(rows):
    """Rows of the published workload sizes (allocation rows keep it in their header)."""
    return [r for r in rows if r.get('queries', (r.get('header') or {}).get('queries')) in SIZES]


def ratio(numerator, denominator):
    if numerator is None or denominator in (None, 0):
        return None
    return numerator / denominator


def pct_change(after, before):
    value = ratio(after, before)
    return None if value is None else (value - 1) * 100


def reduction(after, before):
    value = ratio(after, before)
    return None if value is None else (1 - value) * 100


def quantile(sorted_values, p):
    """Nearest lower rank, floor((n - 1) * p), matching the harness."""
    return sorted_values[int((len(sorted_values) - 1) * p)]


def median(values):
    return statistics.median(values) if values else None


def bootstrap_median_ci(values, rng, iterations=BOOTSTRAP):
    n = len(values)
    if n < 2:
        return None, None
    estimates = sorted(statistics.median(rng.choices(values, k=n)) for _ in range(iterations))
    return estimates[int(0.025 * iterations)], estimates[int(0.975 * iterations) - 1]


def bootstrap_ratio_ci(after, before, rng, iterations=BOOTSTRAP):
    """95% percentile interval of median(after) / median(before)."""
    if len(after) < 2 or len(before) < 2:
        return None, None
    estimates = []
    for _ in range(iterations):
        b = statistics.median(rng.choices(before, k=len(before)))
        a = statistics.median(rng.choices(after, k=len(after)))
        if b:
            estimates.append(a / b)
    estimates.sort()
    return estimates[int(0.025 * len(estimates))], estimates[int(0.975 * len(estimates)) - 1]


def describe(values, rng=None):
    """Summary statistics; a bootstrap CI of the median only when rng is given."""
    values = [v for v in values if v is not None]
    if not values:
        return None
    ordered = sorted(values)
    low, high = bootstrap_median_ci(values, rng) if rng else (None, None)
    mean = statistics.fmean(values)
    stdev = statistics.stdev(values) if len(values) > 1 else 0.0
    return {'n': len(values), 'median': statistics.median(values), 'mean': mean,
            'stdev': stdev, 'cv_percent': 100 * stdev / mean if mean else None,
            'min': ordered[0], 'max': ordered[-1], 'q1': quantile(ordered, 0.25),
            'q3': quantile(ordered, 0.75), 'ci95_low': low, 'ci95_high': high}


def flatten_run(row):
    """Scalar per-run fields for CSV/JSON publication."""
    workers = row.get('workers', [])
    flat = {k: v for k, v in row.items()
            if k not in ('workers', 'latency_histogram_ns', 'latency_ns',
                         'status_after_warmup', 'status_after_lookups')}
    flat['m_lookups_per_second'] = row['lookups_per_second'] / 1e6
    flat['elapsed_ms'] = row['elapsed_ns'] / 1e6
    threads = row['threads']
    flat['cpu_utilization_percent'] = 100 * row['cpu_ns'] / (row['elapsed_ns'] * threads)
    flat['core_cycles_per_lookup'] = row['aperf'] / row['completed'] if row.get('aperf') else None
    flat['effective_ghz'] = (row['aperf'] / threads / row['elapsed_ns']) if row.get('aperf') else None
    flat['worker_cpus'] = ' '.join(str(w['cpu']) for w in workers)
    flat['slowest_worker_ms'] = max(w['elapsed_ns'] for w in workers) / 1e6 if workers else None
    flat['fastest_worker_ms'] = min(w['elapsed_ns'] for w in workers) / 1e6 if workers else None
    for stage in ('status_after_warmup', 'status_after_lookups'):
        for key, value in row.get(stage, {}).items():
            flat[f'{stage}_{key}'] = value
    for key, value in (row.get('latency_ns') or {}).items():
        flat[f'latency_{key}_ns'] = value
    return flat


def merge_histograms(runs):
    merged = defaultdict(int)
    for run in runs:
        for value, count in run.get('latency_histogram_ns', []):
            merged[value] += count
    return merged


def histogram_quantiles(histogram):
    total = sum(histogram.values())
    if not total:
        return None
    points = {'min': 0.0, 'p50': 0.5, 'p90': 0.9, 'p95': 0.95, 'p99': 0.99, 'p999': 0.999}
    targets = sorted((int((total - 1) * p), name) for name, p in points.items())
    result, seen, index = {}, 0, 0
    for value in sorted(histogram):
        seen += histogram[value]
        while index < len(targets) and targets[index][0] < seen:
            result[targets[index][1]] = value
            index += 1
    result['max'] = max(histogram)
    result['observations'] = total
    result['mean'] = sum(v * c for v, c in histogram.items()) / total
    return result


def config_id(*parts):
    return '|'.join(str(p) for p in parts)


def analyze_paired(rows, campaign, rng):
    groups = defaultdict(lambda: {'before': [], 'after': []})
    for row in rows:
        key = (row['database'], row['profile'], row['operation'], row['family'],
               row['queries'], row['threads'])
        groups[key][row['version']].append(row)
    results = []
    for key in sorted(groups):
        database, profile, operation, family, queries, threads = key
        runs = groups[key]
        entry = {'id': config_id(campaign, *key), 'campaign': campaign,
                 'database': database, 'profile': profile, 'operation': operation,
                 'family': family, 'queries': queries, 'threads': threads,
                 'corpus_sha256': runs['before'][0]['corpus_sha256'],
                 'decode_path': runs['before'][0]['decode_path'],
                 'oversubscribed': runs['before'][0]['oversubscribed'],
                 'worker_cpus': [w['cpu'] for w in runs['before'][0]['workers']],
                 'hits': runs['before'][0]['hits'], 'misses': runs['before'][0]['misses'],
                 'decoded': runs['before'][0]['decoded'],
                 'checksums_identical': len({(r['checksum'], r['hits'], r['decoded'])
                                             for r in runs['before'] + runs['after']}) == 1,
                 'completed_ok': all(r['completed'] == queries and r['errors'] == 0 and
                                     r['decode_errors'] == 0
                                     for r in runs['before'] + runs['after'])}
        entry['hit_ratio'] = entry['hits'] / queries
        for version in VERSIONS:
            vr = sorted(runs[version], key=lambda r: r['pair'])
            stats = {
                'runs': len(vr),
                'throughput_mlps': describe([r['lookups_per_second'] / 1e6 for r in vr], rng),
                'elapsed_ms': describe([r['elapsed_ns'] / 1e6 for r in vr]),
                'cpu_utilization_percent': describe(
                    [100 * r['cpu_ns'] / (r['elapsed_ns'] * threads) for r in vr]),
                'core_cycles_per_lookup': describe(
                    [r['aperf'] / r['completed'] for r in vr if r.get('aperf')]),
                'effective_ghz': describe(
                    [r['aperf'] / threads / r['elapsed_ns'] for r in vr if r.get('aperf')]),
                'involuntary_switches_max': max(r['involuntary_switches'] for r in vr),
                'voluntary_switches_max': max(r['voluntary_switches'] for r in vr),
                'peak_rss_mib': describe([r['status_after_lookups']['vm_hwm_kib'] / 1024 for r in vr]),
                'per_run_mlps': [r['lookups_per_second'] / 1e6 for r in vr],
            }
            if campaign == 'latency':
                pooled = histogram_quantiles(merge_histograms(vr))
                stats['latency_pooled_ns'] = pooled
                stats['latency_per_run_p50'] = describe([r['latency_ns']['p50'] for r in vr])
                stats['latency_per_run_p99'] = describe([r['latency_ns']['p99'] for r in vr])
                stats['latency_per_run'] = [r['latency_ns'] for r in vr]
            entry[version] = stats
        before = entry['before']['throughput_mlps']['median']
        after = entry['after']['throughput_mlps']['median']
        low, high = bootstrap_ratio_ci([r['lookups_per_second'] for r in runs['after']],
                                       [r['lookups_per_second'] for r in runs['before']], rng)
        paired = {}
        for r in runs['before']:
            paired.setdefault(r['pair'], {})['before'] = r['lookups_per_second']
        for r in runs['after']:
            paired.setdefault(r['pair'], {})['after'] = r['lookups_per_second']
        paired_ratios = [p['after'] / p['before'] for p in paired.values() if len(p) == 2]
        entry['comparison'] = {
            'speedup': ratio(after, before),
            'change_percent': pct_change(after, before),
            'speedup_ci95_low': low, 'speedup_ci95_high': high,
            'paired_ratio_median': median(paired_ratios),
            'paired_ratio_min': min(paired_ratios) if paired_ratios else None,
            'paired_ratio_max': max(paired_ratios) if paired_ratios else None,
            'conclusive': low is not None and (low > 1 or high < 1),
            'direction': ('faster' if low and low > 1 else
                          'slower' if high and high < 1 else 'inconclusive'),
            'elapsed_reduction_percent': reduction(entry['after']['elapsed_ms']['median'],
                                                   entry['before']['elapsed_ms']['median']),
        }
        if campaign == 'latency':
            lat = {}
            for name in ('min', 'p50', 'p90', 'p95', 'p99', 'p999', 'max', 'mean'):
                b = entry['before']['latency_pooled_ns'][name]
                a = entry['after']['latency_pooled_ns'][name]
                lat[name] = {'before': b, 'after': a, 'reduction_percent': reduction(a, b)}
            entry['latency_comparison'] = lat
        results.append(entry)
    return results


def add_scaling(configs):
    index = {(c['campaign'], c['database'], c['profile'], c['operation'], c['family'],
              c['queries'], c['threads']): c for c in configs}
    for c in configs:
        base = index.get((c['campaign'], c['database'], c['profile'], c['operation'],
                          c['family'], c['queries'], 1))
        c['scaling'] = {}
        for version in VERSIONS:
            one = base[version]['throughput_mlps']['median'] if base else None
            mine = c[version]['throughput_mlps']['median']
            scaling = ratio(mine, one)
            c['scaling'][version] = {
                'scaling': scaling,
                'efficiency_percent': None if scaling is None else scaling / c['threads'] * 100}


def analyze_attribution(rows, rng):
    """Before vs After with MMDB_NO_PREPARE vs After, same corpora and order rotation."""
    groups = defaultdict(lambda: defaultdict(list))
    for row in rows:
        groups[(row['database'], row['profile'], row['family'], row['queries'], row['threads'])][row['version']].append(row)
    out = []
    for key in sorted(groups):
        database, profile, family, queries, threads = key
        runs = groups[key]
        entry = {'database': database, 'profile': profile, 'operation': 'lookup', 'family': family,
                 'queries': queries, 'threads': threads,
                 'checksums_identical': len({(r['checksum'], r['hits']) for v in runs.values() for r in v}) == 1}
        values = {}
        for variant in ('before', 'after-packed', 'after'):
            values[variant] = [r['lookups_per_second'] / 1e6 for r in runs[variant]]
            entry[variant] = describe(values[variant], rng)
        for name, numerator, denominator in (('packed_vs_before', 'after-packed', 'before'),
                                             ('after_vs_packed', 'after', 'after-packed'),
                                             ('after_vs_before', 'after', 'before')):
            low, high = bootstrap_ratio_ci(values[numerator], values[denominator], rng)
            entry[name] = {'ratio': ratio(entry[numerator]['median'], entry[denominator]['median']),
                           'ci95_low': low, 'ci95_high': high}
        total = entry['after_vs_before']['ratio']
        packed = entry['packed_vs_before']['ratio']
        entry['packed_share_of_log_gain'] = (math.log(packed) / math.log(total)
                                             if total and packed and total > 1 else None)
        out.append(entry)
    return out


def analyze_memory(rows, rng):
    groups = defaultdict(lambda: {'before': [], 'after': []})
    for row in rows:
        groups[(row['database'], row['family'], row['queries'], row['threads'])][row['version']].append(row)
    out = []
    for key in sorted(groups):
        database, family, queries, threads = key
        entry = {'database': database, 'family': family, 'queries': queries, 'threads': threads,
                 'profile': 'random', 'operation': 'lookup'}
        for version in VERSIONS:
            vr = groups[key][version]
            def mib(values):
                return describe([v / 1024 for v in values])
            h = [r['header'] for r in vr]
            res = [r['result'] for r in vr]
            entry[version] = {
                'iterations': len(vr),
                'rss_initial_mib': mib([x['status_initial']['vm_rss_kib'] for x in h]),
                'rss_after_open_mib': mib([x['status_after_open']['vm_rss_kib'] for x in h]),
                'rss_anon_after_open_mib': mib([x['status_after_open']['rss_anon_kib'] for x in h]),
                'rss_file_after_open_mib': mib([x['status_after_open']['rss_file_kib'] for x in h]),
                'mapping_rss_after_open_mib': mib([x['mapping_rss_after_open_kib'] for x in h]),
                'heap_after_open_mib': describe(
                    [(x['heap_after_open'] - x['heap_before_open']) / 2**20 for x in h]),
                'rss_after_corpus_mib': mib([x['status_after_corpus']['vm_rss_kib'] for x in h]),
                'corpus_mib': h[0]['corpus_bytes'] / 2**20,
                'rss_after_warmup_mib': mib([x['status_after_warmup']['vm_rss_kib'] for x in res]),
                'rss_anon_after_warmup_mib': mib([x['status_after_warmup']['rss_anon_kib'] for x in res]),
                'mapping_rss_after_warmup_mib': mib([x['mapping_rss_after_warmup_kib'] for x in res]),
                'peak_rss_lookups_mib': mib([x['status_after_lookups']['vm_hwm_kib'] for x in res]),
                'peak_reset_ok': all(x['peak_reset'] for x in res),
            }
        comp = {}
        for name in ('rss_after_open_mib', 'rss_after_warmup_mib', 'peak_rss_lookups_mib',
                     'heap_after_open_mib', 'mapping_rss_after_open_mib'):
            b = entry['before'][name]['median']
            a = entry['after'][name]['median']
            comp[name] = {'before': b, 'after': a, 'delta': a - b,
                          'change_percent': pct_change(a, b)}
        entry['comparison'] = comp
        out.append(entry)
    return out


def analyze_startup(rows, rng):
    groups = defaultdict(list)
    for row in rows:
        groups[(row['database'], row['version'], row['variant'], row['cold'])].append(row)
    out = []
    for key in sorted(groups):
        database, version, variant, cold = key
        vr = groups[key]
        out.append({
            'database': database, 'version': version, 'variant': variant, 'cold': cold,
            'open_ms': describe([r['open_ns'] / 1e6 for r in vr], rng),
            'context_ms': describe([r['context_ns'] / 1e6 for r in vr], rng) if variant == 'context' else None,
            'total_ms': describe([(r['open_ns'] + r['context_ns']) / 1e6 for r in vr]),
            'heap_after_open_mib': describe([(r['heap_after_open'] - r['heap_before_open']) / 2**20 for r in vr]),
            'rss_after_open_mib': describe([r['status_after_open']['vm_rss_kib'] / 1024 for r in vr]),
            'mapping_rss_mib': describe([r['mapping_rss_kib'] / 1024 for r in vr]),
            'resident_pages_before_open_max': max(r['resident_pages_before_open'] for r in vr),
            'database_pages': vr[0]['database_pages'],
            'prepared': vr[0]['private_prepared_flag'],
        })
    return out


def analyze_allocations(rows):
    groups = defaultdict(list)
    for row in rows:
        groups[(row['database'], row['family'], row['profile'], row['operation'], row['header']['queries'],
                row['version'])].append(row)
    out = []
    for key in sorted(groups):
        database, family, profile, operation, queries, version = key
        vr = groups[key]
        res = [r['result'] for r in vr]
        completed = sum(r['completed'] for r in res)
        hits = sum(r['hits'] for r in res)
        out.append({
            'database': database, 'family': family, 'profile': profile, 'operation': operation,
            'queries': queries, 'version': version, 'runs': len(vr),
            'allocations_per_lookup': sum(r['measured_alloc_calls'] for r in res) / completed,
            'bytes_per_lookup': sum(r['measured_alloc_bytes'] for r in res) / completed,
            'frees_per_lookup': sum(r['measured_free_calls'] for r in res) / completed,
            'allocations_per_hit': ratio(sum(r['measured_alloc_calls'] for r in res), hits),
            'open_alloc_calls': vr[0]['header']['open_alloc_calls'],
            'open_alloc_bytes': vr[0]['header']['open_alloc_bytes'],
            'hit_ratio': hits / completed,
        })
    return out


def workloads(throughput_headers, verify_rows):
    out = {}
    for h in throughput_headers:
        key = config_id(h['database'], h['family'], h['profile'], h['queries'])
        item = out.setdefault(key, {
            'database': h['database'], 'family': h['family'], 'profile': h['profile'],
            'queries': h['queries'], 'corpus_sha256': h['corpus_sha256'],
            'corpus_bytes': h['corpus_bytes'], 'query_size': h['query_size'],
            'seed': h['seed'], 'populated_leaves': h['populated_leaves'],
            'sampled_networks': h['sampled_networks'],
            'prefix_histogram': h.get('prefix_histogram'), 'corpus_sha256_set': set()})
        item['corpus_sha256_set'].add(h['corpus_sha256'])
    for v in verify_rows:
        key = config_id(v['database'], v['family'], v['profile'], v['queries'])
        item = out.setdefault(key, {'database': v['database'], 'family': v['family'],
                                    'profile': v['profile'], 'queries': v['queries'],
                                    'corpus_sha256': v['corpus_sha256'],
                                    'corpus_sha256_set': set()})
        item['corpus_sha256_set'].add(v['corpus_sha256'])
        item.setdefault('verify', {})[v['version']] = {
            'result_sha256': v['result_sha256'], 'hits': v['verify_hits'],
            'with_data': v['verify_with_data'], 'errors': v['verify_errors'],
            'decode_errors': v['verify_decode_errors']}
    for item in out.values():
        item['corpus_consistent'] = len(item.pop('corpus_sha256_set')) == 1
        verify = item.get('verify')
        if verify:
            item['differential_identical'] = (verify.get('before', {}).get('result_sha256') ==
                                              verify.get('after', {}).get('result_sha256'))
            item['hit_ratio'] = verify['before']['hits'] / item['queries']
            item['hits'] = verify['before']['hits']
            item['misses'] = item['queries'] - verify['before']['hits']
            item['field_present'] = verify['before']['with_data']
    return out


def host_summary(path):
    records = jsonl(path)
    if not records:
        return None
    busy = sorted(r['system_cpu_busy'] for r in records)
    load = sorted(float(r['loadavg'][0]) for r in records)
    names = defaultdict(list)
    for r in records:
        for p in r.get('top_processes', []):
            # The benchmark's own processes are not background activity.
            if not p['name'].startswith('bench-'):
                names[p['name']].append(p['cpu_percent'])
    samples = sum(1 for r in records if 'top_processes' in r)
    top = sorted(((sum(v) / max(1, samples), k) for k, v in names.items()), reverse=True)[:10]
    return {'samples': len(records), 'first': records[0]['time'], 'last': records[-1]['time'],
            'system_cpu_busy_median': quantile(busy, 0.5),
            'system_cpu_busy_p90': quantile(busy, 0.9), 'system_cpu_busy_max': busy[-1],
            'loadavg1_median': quantile(load, 0.5), 'loadavg1_max': load[-1],
            'top_processes_mean_cpu_percent': [{'name': n, 'cpu_percent': round(c, 1)} for c, n in top]}


def timer_granularity(rows):
    """Share of sampled latencies on a 10 ns grid (allowing the +1 ns rounding)."""
    total = on_grid = 0
    for row in rows:
        for value, count in row.get('latency_histogram_ns', []):
            total += count
            on_grid += count if value % 10 in (0, 1) else 0
    return {'observations': total, 'on_10ns_grid_fraction': on_grid / total if total else None}


def geomean(values):
    values = [v for v in values if v]
    return math.exp(sum(math.log(v) for v in values) / len(values)) if values else None


def headline(throughput, latency, rng):
    def subset(predicate):
        return [c for c in throughput if predicate(c)]
    groups = {
        'all': subset(lambda c: True),
        'city': subset(lambda c: c['database'] == 'city'),
        'synthetic': subset(lambda c: c['database'] == 'synthetic'),
        'city_random_lookup': subset(lambda c: c['database'] == 'city' and c['profile'] == 'random' and c['operation'] == 'lookup'),
        'synthetic_random_lookup': subset(lambda c: c['database'] == 'synthetic' and c['profile'] == 'random' and c['operation'] == 'lookup'),
    }
    out = {}
    for name, configs in groups.items():
        if not configs:
            continue
        speedups = [c['comparison']['speedup'] for c in configs]
        # Joint bootstrap: resample runs within every configuration.
        estimates = []
        for _ in range(2000):
            logs = []
            for c in configs:
                b = c['before']['per_run_mlps']
                a = c['after']['per_run_mlps']
                logs.append(math.log(statistics.median(rng.choices(a, k=len(a))) /
                                     statistics.median(rng.choices(b, k=len(b)))))
            estimates.append(math.exp(sum(logs) / len(logs)))
        estimates.sort()
        g = geomean(speedups)
        out[name] = {
            'configurations': len(configs), 'geomean_speedup': g,
            'geomean_change_percent': (g - 1) * 100,
            'geomean_ci95_low': estimates[int(0.025 * len(estimates))],
            'geomean_ci95_high': estimates[int(0.975 * len(estimates)) - 1],
            'min_speedup': min(speedups), 'max_speedup': max(speedups),
            'faster': sum(1 for c in configs if c['comparison']['direction'] == 'faster'),
            'slower': sum(1 for c in configs if c['comparison']['direction'] == 'slower'),
            'inconclusive': sum(1 for c in configs if c['comparison']['direction'] == 'inconclusive'),
        }
    lat = {}
    for name, predicate in (('all', lambda c: True), ('city', lambda c: c['database'] == 'city')):
        configs = [c for c in latency if predicate(c)]
        if configs:
            lat[name] = {q: {'geomean_ratio': geomean([c['latency_comparison'][q]['after'] / c['latency_comparison'][q]['before']
                                                       for c in configs if c['latency_comparison'][q]['before']]),
                             'improved': sum(1 for c in configs if c['latency_comparison'][q]['after'] < c['latency_comparison'][q]['before']),
                             'worse': sum(1 for c in configs if c['latency_comparison'][q]['after'] > c['latency_comparison'][q]['before']),
                             'equal': sum(1 for c in configs if c['latency_comparison'][q]['after'] == c['latency_comparison'][q]['before'])}
                         for q in ('p50', 'p95', 'p99')}
            lat[name]['configurations'] = len(configs)
    return {'throughput': out, 'latency': lat}


def write_csv(path, rows):
    if not rows:
        return
    fields = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    with open(path, 'w', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: (json.dumps(v) if isinstance(v, (list, dict)) else v)
                             for k, v in row.items()})


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def database_metadata(headers, environment):
    """Metadata of each measured database as reported by the harness, with the file's SHA-256."""
    file_of = {name: file for file, name in DATABASE_FILES.items()}
    files = environment.get('files') or {}
    result = {}
    for h in headers:
        name = DATABASE_FILES.get(Path(h['database']).name, h['database'])
        meta = {'type': h['database_type'], 'node_count': h['node_count'], 'record_size': h['record_size'],
                'ip_version': h['ip_version'], 'build_epoch': h['build_epoch'],
                'build_date': datetime.datetime.fromtimestamp(h['build_epoch'], datetime.UTC).date().isoformat(),
                'sha256': (files.get(f'data/{file_of[name]}') or {}).get('sha256')}
        if result.setdefault(name, meta) != meta:
            raise SystemExit(f'{name}: the run headers disagree on the database metadata')
    return result


def archived_commit(tar):
    """Return the commit that git archive recorded in a source snapshot, if any."""
    if not tar.exists():
        return None
    with tar.open('rb') as f:
        result = subprocess.run(['git', 'get-tar-commit-id'], stdin=f, capture_output=True, text=True)
    return result.stdout.strip() or None


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--raw', default=str(DEFAULT_RAW))
    parser.add_argument('--work', default=str(DEFAULT_WORK))
    parser.add_argument('--data', default=str(DEFAULT_DATA))
    args = parser.parse_args()
    raw, work, data = Path(args.raw), Path(args.work), Path(args.data)
    data.mkdir(parents=True, exist_ok=True)
    rng = random.Random(BOOTSTRAP_SEED)

    def load(name):
        rows = jsonl(raw / name)
        return rows if name.startswith('startup/') else published(rows)

    throughput_rows = load('throughput/runs.jsonl')
    latency_rows = load('latency/runs.jsonl')
    throughput = analyze_paired(throughput_rows, 'throughput', rng)
    latency = analyze_paired(latency_rows, 'latency', rng)
    add_scaling(throughput)
    add_scaling(latency)
    memory_rows = load('memory/runs.jsonl')
    memory = analyze_memory(memory_rows, rng)
    attribution_rows = load('attribution/runs.jsonl')
    attribution = analyze_attribution(attribution_rows, rng)
    startup = analyze_startup(load('startup/runs.jsonl'), rng)
    # Campaigns run before the 10M-only change counted allocations over 1M queries; keep those rather than none.
    allocation_rows = load('allocations/runs.jsonl') or jsonl(raw / 'allocations/runs.jsonl')
    allocations = analyze_allocations(allocation_rows)
    verify_rows = load('verify/runs.jsonl')
    headers = load('throughput/headers.jsonl') + load('latency/headers.jsonl')
    work_items = workloads(headers, verify_rows)

    environment = json.loads((raw / 'environment-start.json').read_text())
    end = raw / 'environment-end.json'
    provenance = work / 'provenance'
    summary = {
        'generated_from': str(raw),
        'before': {'commit': BEFORE_SHA, 'label': 'Before', 'description': 'maxmind/libmaxminddb main at 88ad5f4 (Merge pull request #512)'},
        'after': {'commit': AFTER_SHA, 'label': 'After', 'commits': list(AFTER_COMMITS), 'tree': AFTER_TREE,
                  'built_from': archived_commit(work / 'src' / 'after.tar') or AFTER_SHA,
                  'description': f'perf/cache-efficient-lookups at {AFTER_SHA[:7]} ('
                                 + ' + '.join(c[:7] for c in AFTER_COMMITS) + ')',
                  'working_tree_status': (provenance / 'working-tree-status.txt').read_text().splitlines()
                  if (provenance / 'working-tree-status.txt').exists() else None},
        'environment': environment,
        'environment_end': json.loads(end.read_text()) if end.exists() else None,
        'build': {
            'compiler': (provenance / 'compiler.txt').read_text().splitlines() if (provenance / 'compiler.txt').exists() else None,
            'bench_commands': {p.stem.replace('compile-', ''): p.read_text().strip()
                               for p in sorted(provenance.glob('compile-bench-*.txt'))},
            'library_compile_commands': {
                v: next((c['command'] for c in json.loads((provenance / f'compile_commands-{v}.json').read_text())
                         if c['file'].endswith('src/maxminddb.c')), None)
                for v in VERSIONS if (provenance / f'compile_commands-{v}.json').exists()},
            'sha256sums': (provenance / 'SHA256SUMS').read_text().splitlines() if (provenance / 'SHA256SUMS').exists() else None,
            'ldd': (provenance / 'ldd.txt').read_text() if (provenance / 'ldd.txt').exists() else None,
        },
        'protocol': {
            'repetitions': max((c['before']['runs'] for c in throughput), default=0),
            'warmup_queries_per_repetition': throughput_rows[0]['warmup'] if throughput_rows else None,
            'latency_sampling_stride': latency_rows[0]['stride'] if latency_rows else None,
            'bootstrap_iterations': BOOTSTRAP, 'bootstrap_seed': BOOTSTRAP_SEED,
            'timer_ns': headers[0]['timer_ns'] if headers else None,
            'timer_ns_all': sorted({json.dumps(h['timer_ns'], sort_keys=True) for h in headers}),
            'timer_granularity': timer_granularity(latency_rows),
        },
        'tests': json.loads((data / 'tests.json').read_text()) if (data / 'tests.json').exists() else None,
        'throughput': throughput,
        'latency': latency,
        'memory': memory,
        'attribution': attribution,
        'startup': startup,
        'allocations': allocations,
        'workloads': work_items,
        'databases': database_metadata(headers + [r['header'] for r in memory_rows + allocation_rows], environment),
        'host_activity': host_summary(raw / 'host-activity.jsonl'),
        'headline': headline(throughput, latency, random.Random(BOOTSTRAP_SEED + 1)),
        'validation': {
            'throughput_configs': len(throughput), 'latency_configs': len(latency),
            'all_checksums_identical': all(c['checksums_identical'] for c in throughput + latency),
            'all_completed_ok': all(c['completed_ok'] for c in throughput + latency),
            'all_corpora_consistent': all(w['corpus_consistent'] for w in work_items.values()),
            'differential_corpora': sum(1 for w in work_items.values() if 'differential_identical' in w),
            'differential_identical': all(w.get('differential_identical', True) for w in work_items.values()),
            'measured_runs': len(throughput_rows) + len(latency_rows),
            'measured_lookups': sum(r['completed'] for r in throughput_rows + latency_rows),
            'memory_peak_reset_ok': all(m[v]['peak_reset_ok'] for m in memory for v in VERSIONS),
            'startup_cold_evicted': all(s['resident_pages_before_open_max'] == 0 for s in startup if s['cold']),
        },
    }
    (data / 'summary.json').write_text(json.dumps(summary, indent=1) + '\n')

    # Raw per-run publications.
    flat = [flatten_run(r) for r in throughput_rows + latency_rows]
    write_csv(data / 'runs-throughput.csv', [flatten_run(r) for r in throughput_rows])
    write_csv(data / 'runs-latency.csv', [flatten_run(r) for r in latency_rows])
    (data / 'runs.json').write_text(json.dumps(flat, indent=1) + '\n')
    (data / 'latency-histograms.json').write_text(json.dumps([
        {k: r[k] for k in ('database', 'profile', 'operation', 'family', 'queries', 'threads',
                           'version', 'pair', 'latency_observations', 'latency_ns',
                           'latency_histogram_ns')} for r in latency_rows]) + '\n')
    (data / 'workers.json').write_text(json.dumps([
        {k: r[k] for k in ('campaign', 'database', 'profile', 'operation', 'family', 'queries',
                           'threads', 'version', 'pair', 'workers')} for r in throughput_rows + latency_rows]) + '\n')
    if attribution_rows:
        write_csv(data / 'runs-attribution.csv', [
            {k: v for k, v in r.items() if k not in ('workers', 'status_after_warmup', 'status_after_lookups')}
            for r in attribution_rows])
        (data / 'attribution-runs.json').write_text(json.dumps(attribution_rows, indent=1) + '\n')
    published_rows = {'memory': memory_rows, 'startup': load('startup/runs.jsonl'),
                      'allocations': allocation_rows, 'verify': verify_rows}
    for name, rows in published_rows.items():
        (data / f'{name}-runs.json').write_text(json.dumps(rows, indent=1) + '\n')
        flat_rows = []
        for r in rows:
            item = {k: v for k, v in r.items() if k not in ('header', 'result', 'argv')}
            for part in ('header', 'result'):
                for k, v in (r.get(part) or {}).items():
                    if k not in ('prefix_histogram', 'workers', 'cpus'):
                        item[f'{part}_{k}'] = v
            flat_rows.append(item)
        write_csv(data / f'{name}-runs.csv', flat_rows)
    (data / 'environment.json').write_text(json.dumps({
        'start': environment, 'end': summary['environment_end'], 'build': summary['build'],
        'before': summary['before'], 'after': summary['after']}, indent=1) + '\n')
    (data / 'workloads.json').write_text(json.dumps(work_items, indent=1) + '\n')
    if (raw / 'host-activity.jsonl').exists():
        shutil.copy(raw / 'host-activity.jsonl', data / 'host-activity.jsonl')
    sums = [f'{sha256(p)}  {p.relative_to(data)}' for p in sorted(data.rglob('*'))
            if p.is_file() and p.name != 'SHA256SUMS']
    (data / 'SHA256SUMS').write_text('\n'.join(sums) + '\n')
    v = summary['validation']
    failed = [k for k, ok in v.items() if ok is False]
    city = summary['headline']['throughput'].get('city')
    print(f'{v["throughput_configs"]} throughput and {v["latency_configs"]} latency configurations, '
          f'{v["measured_runs"]:,} measured runs; validation: {"FAILED " + ", ".join(failed) if failed else "all checks true"}'
          + (f'; GeoLite2-City geometric mean ×{city["geomean_speedup"]:.2f}' if city else ''))


if __name__ == '__main__':
    main()
