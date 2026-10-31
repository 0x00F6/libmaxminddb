#!/usr/bin/env python3
"""Generate the HTML report, chart exports, pull request draft and
image manifest from performance-comparison-data/summary.json.

Reports can be regenerated from saved results without rerunning measurements:
    python3 benchmarks/reporting/report.py
PNG files are rendered afterwards by render.py from the exported SVG files.
"""
import argparse
import datetime
import hashlib
import html
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import charts as C  # noqa: E402
import diagram  # noqa: E402
from charts import (fmt_mib, fmt_mlps, fmt_ms, fmt_ns, fmt_pct, fmt_speedup, THEME)  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
REPORTS = ROOT / 'benchmarks/reports'
DATA = REPORTS / 'performance-comparison-data'


def set_output(base):
    global REPORTS, ASSETS, HTML_OUT, PR_OUT
    REPORTS = Path(base)
    ASSETS = REPORTS / 'performance-comparison-assets'
    HTML_OUT = REPORTS / 'performance-comparison.html'
    PR_OUT = REPORTS / 'pull-request-description.md'


set_output(REPORTS)

DB_LABEL = {'city': 'GeoLite2-City', 'synthetic': 'Synthetic mixed fixture'}
PROFILE_TITLE = {'random': 'Random', 'hits': 'Populated-Network'}
PROFILE_TEXT = {'random': 'uniformly random addresses',
                'hits': 'addresses inside populated networks'}
OP_TITLE = {'lookup': 'Lookups', 'decode': 'Lookups + Field Decode'}
class SizeLabels(dict):
    def __missing__(self, size):
        return f'{size // 1_000_000}M' if size % 1_000_000 == 0 else f'{size // 1000}K'


SIZE_LABEL = SizeLabels()
# Worker counts of the campaign; configure() reads them from the data.
THREADS = (1, 4, 8)
FAMILIES = (4, 6)
DATABASES = ('city', 'synthetic')
PROFILES = ('random', 'hits')
OPERATIONS = ('lookup', 'decode')
PROFILE_NAME = {'random': 'random', 'hits': 'populated-network'}
OP_NAME = {'lookup': 'lookup', 'decode': 'lookup + decode'}
OP_CALL = {'lookup': '`MMDB_lookup_sockaddr`', 'decode': 'lookup + `MMDB_get_value`'}
# Workload size: 10M queries in the campaign; main() reads it from the data.
SIZE = 10_000_000


def fid(kind, family, size, suffix=''):
    return f'{kind}-ipv{family}-{SIZE_LABEL[size].lower()}{suffix}'


def esc(value):
    return html.escape(str(value), quote=True)


def joined(items):
    items = [str(i) for i in items]
    return items[0] if len(items) == 1 else ', '.join(items[:-1]) + ' and ' + items[-1]


def present(configs, key, order):
    """Values of key found in configs, in the order given."""
    found = {c[key] for c in configs}
    return [v for v in order if v in found]


ARCH_LABEL = {'x86_64': 'x86-64', 'aarch64': 'AArch64'}
CLOCK_LABEL = {'tsc': 'TSC-backed clock', 'arch_sys_counter': 'Arm generic timer',
               'hyperv_clocksource_tsc_page': 'Hyper-V TSC-page clock'}


def plural(count, word):
    return f'{count} {word}{"s" if count != 1 else ""}'


def fmt_hits(ratio):
    percent = ratio * 100
    if 0 < percent < 1:
        return f'{percent:.3f}%'
    return f'{percent:.1f}%'


class Context:
    def __init__(self, summary, key=None, prefix=''):
        self.s = summary
        # Architecture key and the path of its directory, in a merged report.
        self.key = key or summary['environment']['os']['architecture']
        self.prefix = prefix
        env = summary['environment']
        self.cpu = env['cpu']['model']
        self.cores = env['cpu']['physical_cores']
        self.logical = env['cpu']['logical_cpus']
        self.os = env['os']['pretty_name']
        self.kernel = env['os']['kernel']
        self.gcc = env['toolchain']['gcc'].split(')')[-1].strip()
        self.before_short = summary['before']['commit'][:7]
        self.after_short = summary['after']['commit'][:7]
        self.reps = summary['protocol']['repetitions']
        self.versions = {'before': self.before_short, 'after': self.after_short}
        self.db_meta = {}
        for c in summary['throughput']:
            self.db_meta.setdefault(c['database'], {'decode_path': c['decode_path']})
        headers = {}
        for key, item in summary['workloads'].items():
            headers[item['database']] = item
        self.env = env
        self.databases = summary['databases']
        # Databases, profiles and operations of the timed campaigns.
        self.timed_dbs = present(summary['throughput'], 'database', DATABASES)
        self.timed_profiles = present(summary['throughput'], 'profile', PROFILES)
        self.memory_iterations = max(m['before']['iterations'] for m in summary['memory'])
        self.opens = {cold: max(r['open_ms']['n'] for r in summary['startup'] if r['cold'] == cold)
                      for cold in (False, True)}
        cpu = env['cpu']
        self.arch = ARCH_LABEL.get(env['os']['architecture'], env['os']['architecture'])
        self.smt = cpu.get('smt_active') == '1' or (cpu.get('threads_per_core') or 1) > 1
        self.ci = env.get('ci')
        self.virtualization = env['os'].get('virtualization') or 'unknown'
        self.governor = (cpu['topology'][0] if cpu.get('topology') else {}).get('governor')
        # Distinct ACPI CPPC ranks: workers then start on the highest-ranked core.
        self.ranked = len({c.get('cppc_highest_perf') for c in cpu.get('topology') or []} - {None}) > 1
        # Core cycles come from AMD RDPRU (APERF); other CPUs have no user-space cycle counter.
        self.cycles = any(c[v].get('core_cycles_per_lookup') for c in summary['throughput'] for v in ('before', 'after'))
        self.max_threads = max(THREADS)
        self.timer_step = (summary['protocol'].get('timer_granularity') or {}).get('step_ns')

    def host(self):
        """The kind of machine that ran the campaign."""
        if self.ci:
            return 'GitHub-hosted runner'
        if self.virtualization not in ('none', 'unknown'):
            return f'{self.virtualization} virtual machine'
        return 'shared desktop'

    def busiest(self):
        """Which cores the runs with the most workers occupy."""
        cores = self.cores
        return (f'all {cores} physical cores' if self.max_threads >= cores
                else f'{self.max_threads} of the {cores} physical cores')

    def select(self, campaign, **filters):
        return [c for c in self.s[campaign] if all(c[k] == v for k, v in filters.items())]

    def one(self, campaign, **filters):
        found = self.select(campaign, **filters)
        if len(found) != 1:
            raise KeyError((campaign, filters, len(found)))
        return found[0]

    def scope(self, campaign='throughput', **filters):
        """The configurations a campaign covers, in words."""
        configs = self.select(campaign, **filters)
        threads = present(configs, 'threads', THREADS)
        return (f'{joined(f"IPv{f}" for f in present(configs, "family", FAMILIES))}, '
                f'{joined(PROFILE_NAME[p] for p in present(configs, "profile", PROFILES))} addresses, '
                f'{joined(OP_NAME[o] for o in present(configs, "operation", OPERATIONS))}, '
                f'{SIZE_LABEL[SIZE]} queries, {joined(threads)} thread{"s" if threads != [1] else ""}')

    def superlinear(self):
        return [c for c in self.s['throughput']
                if any((c['scaling'][v]['efficiency_percent'] or 0) > 100 for v in ('before', 'after'))]

    def operation_text(self, db, op):
        path = self.db_meta[db]['decode_path'].replace('/', ' → ')
        return ('MMDB_lookup_sockaddr' if op == 'lookup'
                else f'MMDB_lookup_sockaddr + MMDB_get_value({path})')

    def db_detail(self, db):
        d = self.databases[db]
        nodes = f'{d["node_count"]:,} nodes · {d["record_size"]}-bit records'
        if db == 'city':
            return f'GeoLite2-City build {d["build_date"]} · {nodes}'
        return f'Synthetic mixed fixture (generate_synthetic_database.py) · {nodes}'

    def cpu_line(self):
        host = f' · {self.host()}' if self.host() != 'shared desktop' else ''
        return (f'CPU: {self.cpu} · {self.cores} cores / {self.logical} threads ({"SMT on" if self.smt else "no SMT"}){host} · '
                f'workers pinned to distinct physical cores · {self.os} · Linux {self.kernel} · GCC {self.gcc} -O3')

    def data_line(self, db):
        return (f'{self.db_detail(db)} · Before {self.before_short} · After {self.after_short} · '
                'benchmarks/reports/performance-comparison')

    def meta(self, standalone):
        return {'standalone': standalone, 'versions': self.versions}


# Chart builders --------------------------------------------------------------
def throughput_title(family, profile, op, size):
    return f'IPv{family} Throughput — {PROFILE_TITLE[profile]} {OP_TITLE[op]} ({SIZE_LABEL[size]})'


def candlestick_title(family, op):
    return f'Candlestick Percentile Rank — IPv{family} {OP_TITLE[op]}'


def build_throughput(ctx, db, profile, op, family, size, standalone):
    configs = sorted(ctx.select('throughput', database=db, profile=profile, operation=op,
                                family=family, queries=size), key=lambda c: c['threads'])
    subtitle = (f'{DB_LABEL[db]} · {PROFILE_TEXT[profile]} ({fmt_hits(configs[0]["hit_ratio"])} hits) · '
                f'{ctx.operation_text(db, op)} · {size:,} queries total')
    footer = [f'Bars: median of {ctx.reps} repetitions · error bars: 95% bootstrap CI of the median · '
              'speedup = After median / Before median · fixed total work split across '
              f'{", ".join(str(t) for t in THREADS)} workers',
              ctx.cpu_line(), ctx.data_line(db)]
    return C.throughput_chart(ctx.meta(standalone), configs, throughput_title(family, profile, op, size),
                              subtitle, footer)


def build_candlestick(ctx, db, profile, op, family, size, standalone):
    configs = sorted(ctx.select('latency', database=db, profile=profile, operation=op,
                                family=family, queries=size), key=lambda c: c['threads'])
    stride = ctx.s['protocol']['latency_sampling_stride']
    obs = configs[0]['before']['latency_pooled_ns']['observations']
    subtitle = (f'{DB_LABEL[db]} · {PROFILE_TEXT[profile]} ({fmt_hits(configs[0]["hit_ratio"])} hits) · '
                f'{ctx.operation_text(db, op)} · {size:,} queries total')
    timer = ctx.s['protocol']['timer_ns']
    steps = f'≈{ctx.timer_step} ns clock steps on this CPU; ' if ctx.timer_step else ''
    footer = [f'Every {stride}th lookup timed with CLOCK_MONOTONIC_RAW ({steps}timer pair ≈{timer["p50"]} ns, '
              f'included) · {ctx.reps} repetitions pooled, workers merged before percentiles · {obs:,} samples per bar',
              ctx.cpu_line(), ctx.data_line(db)]
    title = candlestick_title(family, op) + f' ({SIZE_LABEL[size]})'
    return C.candlestick_chart(ctx.meta(standalone), configs, title, subtitle, footer)


def build_scaling(ctx, db, profile, op, standalone):
    panels = []
    for family in FAMILIES:
        configs = sorted(ctx.select('throughput', database=db, profile=profile, operation=op,
                                    family=family, queries=SIZE), key=lambda c: c['threads'])
        panels.append({'title': f'IPv{family} · {SIZE_LABEL[SIZE]} queries', 'configs': configs})
    title = f'Thread Scaling — {PROFILE_TITLE[profile]} {OP_TITLE[op]}'
    subtitle = (f'{DB_LABEL[db]} · throughput(N) / throughput(1) of the same version · '
                f'labels: scaling at {ctx.max_threads} workers · parallel efficiency')
    background = {'shared desktop': 'desktop background work',
                  'GitHub-hosted runner': 'other work on the runner and its physical host'}.get(ctx.host(), 'other work on the host')
    footer = [f'Medians of {ctx.reps} repetitions · fixed total work · higher is better · '
              f'the {ctx.max_threads}-worker runs use {ctx.busiest()}, so {background} competes with them']
    note = scaling_note(ctx, db, profile, op)
    if note:
        footer.append(note)
    footer += [ctx.cpu_line(), ctx.data_line(db)]
    return C.scaling_chart(ctx.meta(standalone), panels, title, subtitle, footer)


def scaling_note(ctx, db, profile, op):
    """Explain efficiencies above 100% with the measured APERF cycles."""
    configs = ctx.select('throughput', database=db, profile=profile, operation=op)
    over = [c for c in configs if any((c['scaling'][v]['efficiency_percent'] or 0) > 100 for v in ('before', 'after'))]
    if not over:
        return None
    if not ctx.cycles:
        return 'Efficiency above 100%: no cycle counter on this CPU; cause not isolated'
    c8 = max(over, key=lambda c: (c['queries'], c['threads']))
    c1 = ctx.one('throughput', database=db, profile=profile, operation=op, family=c8['family'],
                 queries=c8['queries'], threads=1)
    cyc = lambda c, v: c[v]['core_cycles_per_lookup']['median']
    return (f'Efficiency above 100%: core cycles per lookup per worker fall with more workers (APERF, IPv{c8["family"]} '
            f'{SIZE_LABEL[c8["queries"]]}: Before {cyc(c1, "before"):.0f} → {cyc(c8, "before"):.0f}, After '
            f'{cyc(c1, "after"):.0f} → {cyc(c8, "after"):.0f} at 1 → {c8["threads"]} workers); cause not isolated')


def memory_groups(ctx):
    groups = []
    for db in ('city', 'synthetic'):
        rows = ctx.select('memory', database=db)
        if not rows:
            continue
        open_row = next(r for r in rows if r['family'] == 4 and r['queries'] == SIZE and r['threads'] == 1)
        b, a = (open_row[v]['heap_after_open_mib']['median'] for v in ('before', 'after'))
        groups.append({'label': [f'{DB_LABEL[db]}', 'library heap after MMDB_open'], 'before': b, 'after': a,
                       'change_label': f'+{a - b:,.1f} MiB'})
        b, a = (open_row[v]['rss_after_open_mib']['median'] for v in ('before', 'after'))
        groups.append({'label': [f'{DB_LABEL[db]}', 'process RSS after MMDB_open'], 'before': b, 'after': a,
                       'change_label': f'+{a - b:,.1f} MiB',
                       'tip': {v: [['Database mapping resident', fmt_mib(open_row[v]['mapping_rss_after_open_mib']['median'])],
                                   ['Heap after open', fmt_mib(open_row[v]['heap_after_open_mib']['median'])]]
                               for v in ('before', 'after')}})
        for family in FAMILIES:
            row = next(r for r in rows if r['family'] == family and r['queries'] == SIZE and r['threads'] == 1)
            b, a = (row[v]['peak_rss_lookups_mib']['median'] for v in ('before', 'after'))
            groups.append({'label': [f'{DB_LABEL[db]}', f'peak RSS, {SIZE_LABEL[SIZE]} IPv{family} lookups, 1 thread'],
                           'before': b, 'after': a, 'change_label': f'{a - b:+,.1f} MiB ({fmt_pct((a / b - 1) * 100)})',
                           'tip': {v: [['Query corpus (identical)', fmt_mib(row[v]['corpus_mib'])],
                                       ['RSS after warm-up', fmt_mib(row[v]['rss_after_warmup_mib']['median'])],
                                       ['Database mapping resident after warm-up', fmt_mib(row[v]['mapping_rss_after_warmup_mib']['median'])]]
                                   for v in ('before', 'after')}})
    return groups


def build_memory(ctx, standalone):
    groups = memory_groups(ctx)
    title = 'Memory Usage — Reader State and Process RSS'
    subtitle = (f'Fresh process per measurement · median of {ctx.memory_iterations} · random lookups · '
                'heap = mallinfo2 in-use bytes; RSS = whole process')
    corpus = {r['family']: r['before']['corpus_mib'] for r in ctx.select('memory', database='city', queries=SIZE, threads=1)}
    footer = ['Peak = VmHWM after resetting the high-water mark at the end of warm-up (lookup phase only) · '
              f'peak rows include the identical {SIZE_LABEL[SIZE]} query corpus ('
              + ', '.join(f'{fmt_mib(corpus[f])} IPv{f}' for f in FAMILIES if f in corpus) + ')',
              ctx.cpu_line(),
              f'GeoLite2-City {ctx.databases["city"]["build_date"]} and synthetic fixture · Before {ctx.before_short} · After {ctx.after_short} · '
              'benchmarks/reports/performance-comparison']
    return C.grouped_bars(ctx.meta(standalone), title, subtitle, groups, 'MiB',
                          'Memory (MiB, lower is better)', footer, higher=False,
                          fmt=lambda v: f'{v * 1024:,.1f} KiB' if 0 < v < 0.1 else f'{v:,.1f} MiB')


def startup_groups(ctx):
    groups = []
    for db in ('city', 'synthetic'):
        for cold in (False, True):
            rows = [r for r in ctx.s['startup'] if r['database'] == db and r['cold'] == cold]
            if not rows:
                continue
            get = lambda version, variant: next(r for r in rows if r['version'] == version and r['variant'] == variant)
            b = get('before', 'default')['open_ms']['median']
            a = get('after', 'default')['open_ms']['median']
            nop = get('after', 'no-prepare')['open_ms']['median']
            ctxt = get('after', 'context')['context_ms']['median']
            state = 'cold page cache' if cold else 'warm page cache'
            groups.append({'label': [DB_LABEL[db], f'MMDB_open, {state}'], 'before': b, 'after': a,
                           'change_label': f'+{a - b:,.2f} ms',
                           'tip': {'before': [['Samples', str(get('before', 'default')['open_ms']['n'])]],
                                   'after': [['MMDB_open with MMDB_NO_PREPARE', fmt_ms(nop)],
                                             ['MMDB_create_lookup_context alone', fmt_ms(ctxt)],
                                             ['Samples', str(get('after', 'default')['open_ms']['n'])]]}})
    return groups


def build_startup(ctx, standalone):
    groups = startup_groups(ctx)
    title = 'Startup — Database Opening and Index Preparation'
    subtitle = (f'Fresh process per open · pinned to the fastest core · medians ({ctx.opens[False]} warm, '
                f'{ctx.opens[True]} cold opens per variant) · After includes automatic radix-16 preparation')
    footer = ['Cold = clean file pages evicted with posix_fadvise(DONTNEED) before opening (0 resident pages verified with mincore)',
              ctx.cpu_line(),
              f'GeoLite2-City {ctx.databases["city"]["build_date"]} and synthetic fixture · Before {ctx.before_short} · After {ctx.after_short} · '
              'benchmarks/reports/performance-comparison']
    return C.grouped_bars(ctx.meta(standalone), title, subtitle, groups, 'ms',
                          'Time to open (ms, lower is better)', footer, higher=False, fmt=fmt_ms)


def allocation_groups(ctx):
    groups = []
    for db in ('city', 'synthetic'):
        for family in FAMILIES:
            for profile in ('random', 'hits'):
                rows = [r for r in ctx.s['allocations'] if r['database'] == db and r['family'] == family
                        and r['profile'] == profile and r['operation'] == 'decode']
                if not rows:
                    continue
                get = lambda v: next(r for r in rows if r['version'] == v)
                b, a = get('before'), get('after')
                groups.append({'label': [f'{DB_LABEL[db]} · IPv{family}', f'{PROFILE_TEXT[profile]} ({fmt_hits(b["hit_ratio"])} hits)'],
                               'before': b['allocations_per_lookup'], 'after': a['allocations_per_lookup'],
                               'change_label': f'{b["bytes_per_lookup"]:.1f} → {a["bytes_per_lookup"]:.1f} B/op',
                               'tip': {v: [['Bytes per lookup', f'{get(v)["bytes_per_lookup"]:.2f}'],
                                           ['Allocations per hit', f'{(get(v)["allocations_per_hit"] or 0):.3f}']]
                                       for v in ('before', 'after')}})
    return groups


def build_allocations(ctx, standalone):
    groups = allocation_groups(ctx)
    title = 'Heap Allocations per Lookup + Field Decode'
    sizes = ' / '.join(SIZE_LABEL[q] for q in sorted({r['queries'] for r in ctx.s['allocations']}))
    subtitle = (f'Separate instrumented binaries (-Wl,--wrap=malloc,calloc,realloc,free) · {sizes} queries · 1 thread · '
                'measured lookup phase only')
    footer = ['Lookups without decoding allocate nothing in either version. Counting binaries are never used for timing.',
              ctx.cpu_line(),
              f'MMDB_get_value(country → iso_code) on GeoLite2-City, MMDB_get_value(id) on the synthetic fixture · '
              f'Before {ctx.before_short} · After {ctx.after_short}']
    return C.grouped_bars(ctx.meta(standalone), title, subtitle, groups, 'allocations/op',
                          'Heap allocations per lookup (lower is better)', footer, higher=False,
                          fmt=lambda v: f'{v:.3f}')


def build_matrix(ctx, db, standalone):
    columns = [{'top': SIZE_LABEL[SIZE], 'bottom': f'{t} thread{"s" if t > 1 else ""}', 'threads': t, 'size': SIZE}
               for t in THREADS]
    rows = []
    for profile in ('random', 'hits'):
        for op in ('lookup', 'decode'):
            for family in FAMILIES:
                cells = []
                for col in columns:
                    found = ctx.select('throughput', database=db, profile=profile, operation=op,
                                       family=family, queries=col['size'], threads=col['threads'])
                    if not found:
                        cells.append(None)
                        continue
                    c = found[0]['comparison']
                    cells.append({'speedup': c['speedup'], 'change': c['change_percent'],
                                  'low': c['speedup_ci95_low'], 'high': c['speedup_ci95_high'],
                                  'direction': c['direction'],
                                  'before': found[0]['before']['throughput_mlps']['median'],
                                  'after': found[0]['after']['throughput_mlps']['median']})
                if any(cells):
                    rows.append({'label': f'IPv{family} · {PROFILE_TITLE[profile]} {OP_TITLE[op].lower()}', 'cells': cells})
    head = ctx.s['headline']['throughput'].get(db, {})
    title = f'Throughput Speedup Matrix — {DB_LABEL[db]}'
    subtitle = (f'All {head.get("configurations", 0)} configurations · geometric mean ×{head.get("geomean_speedup", 0):.2f} '
                f'({fmt_pct(head.get("geomean_change_percent"))}) · {head.get("faster", 0)} faster, '
                f'{head.get("slower", 0)} slower, {head.get("inconclusive", 0)} inconclusive')
    footer = [f'Speedup = median After / median Before throughput ({ctx.reps} repetitions each) · higher is better',
              ctx.cpu_line(), ctx.data_line(db)]
    return C.speedup_matrix(ctx.meta(standalone), title, subtitle, rows, columns, footer)


# Exports and manifest ----------------------------------------------------------
RADIX16_TITLE = 'Radix-16 Preparation and Lookup'


def export_specs(ctx):
    specs = []
    for family in FAMILIES:
        specs.append({
            'id': fid('throughput', family, SIZE),
            'title': throughput_title(family, 'random', 'lookup', SIZE),
            'build': lambda s, f=family: build_throughput(ctx, 'city', 'random', 'lookup', f, SIZE, s),
            'kind': 'throughput', 'family': family, 'size': SIZE, 'database': 'city',
            'profile': 'random', 'operation': 'lookup'})
    for family in FAMILIES if 'hits' in ctx.timed_profiles else ():
        specs.append({
            'id': fid('throughput', family, SIZE, '-populated'),
            'title': throughput_title(family, 'hits', 'lookup', SIZE),
            'build': lambda s, f=family: build_throughput(ctx, 'city', 'hits', 'lookup', f, SIZE, s),
            'kind': 'throughput', 'family': family, 'size': SIZE, 'database': 'city',
            'profile': 'hits', 'operation': 'lookup'})
    for family in FAMILIES:
        specs.append({
            'id': fid('candlestick', family, SIZE),
            'title': candlestick_title(family, 'lookup') + f' ({SIZE_LABEL[SIZE]})',
            'build': lambda s, f=family: build_candlestick(ctx, 'city', 'random', 'lookup', f, SIZE, s),
            'kind': 'candlestick', 'family': family, 'size': SIZE, 'database': 'city',
            'profile': 'random', 'operation': 'lookup'})
    specs += [
        {'id': 'memory-usage', 'title': 'Memory Usage — Reader State and Process RSS',
         'build': lambda s: build_memory(ctx, s), 'kind': 'memory'},
        {'id': 'startup-time', 'title': 'Startup — Database Opening and Index Preparation',
         'build': lambda s: build_startup(ctx, s), 'kind': 'startup'},
        {'id': 'thread-scaling', 'title': 'Thread Scaling — Random Lookups',
         'build': lambda s: build_scaling(ctx, 'city', 'random', 'lookup', s), 'kind': 'scaling'},
        {'id': 'allocations', 'title': 'Heap Allocations per Lookup + Field Decode',
         'build': lambda s: build_allocations(ctx, s), 'kind': 'allocations'},
        {'id': 'radix16-preparation', 'title': RADIX16_TITLE, 'kind': 'diagram',
         'build': lambda s: diagram.radix16_diagram(ctx, SIZE, RADIX16_TITLE, [ctx.cpu_line(), ctx.data_line('city')])},
    ]
    for db in ctx.timed_dbs:
        specs.append({'id': f'speedup-matrix-{db}', 'title': f'Throughput Speedup Matrix — {DB_LABEL[db]}',
                      'build': lambda s, d=db: build_matrix(ctx, d, s), 'kind': 'matrix', 'database': db})
    return specs


def caption_for(ctx, spec):
    kind = spec['kind']
    if kind == 'throughput':
        configs = sorted(ctx.select('throughput', database=spec['database'], profile=spec['profile'],
                                    operation=spec['operation'], family=spec['family'], queries=spec['size']),
                         key=lambda c: c['threads'])
        parts = [f'{c["threads"]} thread{"s" if c["threads"] > 1 else ""}: {fmt_mlps(c["before"]["throughput_mlps"]["median"])} → '
                 f'{fmt_mlps(c["after"]["throughput_mlps"]["median"])} M lookups/s ({fmt_speedup(c["comparison"]["speedup"])})'
                 for c in configs]
        return (f'{spec["title"]} on {DB_LABEL[spec["database"]]} ({fmt_hits(configs[0]["hit_ratio"])} hits). '
                + '; '.join(parts) + '.')
    if kind == 'candlestick':
        configs = sorted(ctx.select('latency', database=spec['database'], profile=spec['profile'],
                                    operation=spec['operation'], family=spec['family'], queries=spec['size']),
                         key=lambda c: c['threads'])
        parts = [f'{c["threads"]} thread{"s" if c["threads"] > 1 else ""}: p50 {fmt_ns(c["latency_comparison"]["p50"]["before"])} → '
                 f'{fmt_ns(c["latency_comparison"]["p50"]["after"])}, p99 {fmt_ns(c["latency_comparison"]["p99"]["before"])} → '
                 f'{fmt_ns(c["latency_comparison"]["p99"]["after"])}' for c in configs]
        return (f'Sampled per-lookup latency, random IPv{spec["family"]} lookups on GeoLite2-City, '
                f'{SIZE_LABEL[spec["size"]]} queries. ' + '; '.join(parts) + '.')
    if kind == 'memory':
        groups = memory_groups(ctx)
        g0, g1 = groups[0], groups[1]
        small = lambda v: f'{v * 1024:,.1f} KiB' if 0 < v < 0.1 else fmt_mib(v)
        return (f'GeoLite2-City: library heap after MMDB_open {small(g0["before"])} → {fmt_mib(g0["after"])}; '
                f'process RSS after open {fmt_mib(g1["before"])} → {fmt_mib(g1["after"])}. '
                f'Peak RSS during {SIZE_LABEL[SIZE]}-query lookups is shown with the identical query corpus included.')
    if kind == 'startup':
        groups = startup_groups(ctx)
        g = groups[0]
        return (f'MMDB_open on GeoLite2-City with a warm page cache: {fmt_ms(g["before"])} → {fmt_ms(g["after"])} '
                '(After prepares the native tree and radix-16 tables before returning).')
    if kind == 'scaling':
        return ('Throughput relative to one worker for random GeoLite2-City lookups; both versions are compared '
                f'with ideal linear scaling at {joined(THREADS)} workers.')
    if kind == 'allocations':
        return ('Before allocates one temporary lookup-path array per MMDB_get_value call on a hit; After uses a '
                'stack buffer for paths of up to seven components and allocates nothing.')
    if kind == 'diagram':
        return (f'{ctx.after_short}: MMDB_open converts the packed tree once into aligned native nodes and 16-bit root '
                'tables; a lookup replaces the first 16 tree levels with one table load and walks the rest without '
                'decoding packed records.')
    if kind == 'matrix':
        head = ctx.s['headline']['throughput'][spec['database']]
        return (f'All {head["configurations"]} throughput configurations on {DB_LABEL[spec["database"]]}: geometric mean '
                f'{fmt_speedup(head["geomean_speedup"])}, range {fmt_speedup(head["min_speedup"])}–{fmt_speedup(head["max_speedup"])}.')
    return spec['title']


def alt_for(ctx, spec):
    base = {'throughput': 'Horizontal bar chart comparing Before and After throughput',
            'candlestick': 'Candlestick chart of min, p50, p95 and p99 latency for Before and After',
            'memory': 'Bar chart comparing memory use of Before and After',
            'startup': 'Bar chart comparing database opening time of Before and After',
            'scaling': 'Line charts of thread scaling for Before and After against ideal scaling',
            'allocations': 'Bar chart of heap allocations per lookup for Before and After',
            'matrix': 'Heat table of throughput speedups for every configuration',
            'diagram': 'Diagram of the automatic radix-16 preparation in MMDB_open and of a prepared lookup, with '
                       'why it is faster'}[spec['kind']]
    return f'{base} on {ctx.cpu}: {spec["title"]}'


def write_exports(ctx):
    ASSETS.mkdir(parents=True, exist_ok=True)
    manifest = []
    for spec in export_specs(ctx):
        svg = spec['build'](True)
        path = ASSETS / f'{spec["id"]}.svg'
        path.write_text('<?xml version="1.0" encoding="UTF-8"?>\n' + svg + '\n')
        width = int(svg.split('width="', 1)[1].split('"', 1)[0])
        height = int(svg.split('height="', 1)[1].split('"', 1)[0])
        manifest.append({'id': spec['id'], 'title': spec['title'], 'kind': spec['kind'],
                         'svg': f'performance-comparison-assets/{spec["id"]}.svg',
                         'png': f'performance-comparison-assets/{spec["id"]}.png',
                         'svg_size': [width, height], 'png_size': [2 * width, 2 * height],
                         'alt': alt_for(ctx, spec), 'caption': caption_for(ctx, spec),
                         'hosted_url': None,
                         **{k: spec[k] for k in ('database', 'profile', 'operation', 'family', 'size') if k in spec}})
    return manifest


# HTML report --------------------------------------------------------------------
CSS = """
:root{--bg:#080e12;--surface:#10191f;--surface2:#142127;--line:#223039;--text:#eef3f5;--text2:#c5d0d6;
--muted:#94a5ae;--accent:#55f1b1;--accent-bg:#14372c;--before:#3987e5;--after:#d95926;--good:#0ca30c;
--critical:#e66767;--sans:Inter,'Segoe UI','Noto Sans',system-ui,sans-serif;
--mono:'SFMono-Regular',Consolas,'Noto Sans Mono','DejaVu Sans Mono',monospace;color-scheme:dark}
*{box-sizing:border-box}html{scroll-behavior:smooth}
body{margin:0;background:var(--bg);color:var(--text);font:15px/1.6 var(--sans)}
a{color:var(--accent)}code,kbd{font-family:var(--mono);font-size:.92em;color:var(--text)}
.wrap{max-width:1200px;margin:0 auto;padding:28px 28px 90px}
.eyebrow{font:600 11.5px/1.4 var(--mono);letter-spacing:.12em;text-transform:uppercase;color:var(--muted);display:flex;align-items:center;gap:8px}
.eyebrow:before{content:"";width:7px;height:7px;border-radius:50%;background:var(--accent)}
h1{font-size:clamp(30px,4.2vw,48px);line-height:1.08;letter-spacing:-.02em;margin:14px 0 12px;font-weight:750}
h1 .accent{color:var(--accent)}
h2{font-size:26px;letter-spacing:-.01em;margin:56px 0 6px}h2+p.lead{margin-top:0}
h3{font-size:18px;margin:0 0 2px}
p.lead{color:var(--text2);max-width:880px}
.meta{display:flex;flex-wrap:wrap;gap:10px 22px;padding:12px 16px;border:1px solid var(--line);border-radius:10px;background:var(--surface);font:12.5px/1.5 var(--mono);color:var(--text2);margin:18px 0}
.meta b{color:var(--accent);font-weight:600}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:14px;margin:22px 0 8px}
.card{background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:16px 18px}
.card .label{font:600 11px/1.3 var(--mono);text-transform:uppercase;letter-spacing:.08em;color:var(--muted)}
.card .value{font-size:32px;font-weight:700;margin:8px 0 2px;letter-spacing:-.01em}
.card .detail{color:var(--text2);font-size:13px}
.filters{position:sticky;top:0;z-index:10;background:rgba(8,14,18,.94);backdrop-filter:blur(8px);border-bottom:1px solid var(--line);
display:flex;flex-wrap:wrap;align-items:flex-end;gap:14px 22px;padding:12px 0;margin-top:26px}
.control{display:flex;flex-direction:column;gap:5px}
.control>span{font:600 10.5px/1 var(--mono);letter-spacing:.1em;text-transform:uppercase;color:var(--muted)}
.seg{display:inline-flex;border:1px solid var(--line);border-radius:8px;overflow:hidden;background:var(--surface)}
.seg button{background:transparent;color:var(--text2);border:0;padding:7px 12px;font:600 12.5px/1.2 var(--mono);cursor:pointer}
.seg button+button{border-left:1px solid var(--line)}
.seg button:hover{background:var(--surface2)}
.seg button[aria-pressed=true]{background:var(--accent-bg);color:var(--accent)}
.seg button[hidden]{display:none}
.seg button:focus-visible,.mark:focus-visible,th button:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.chart-card{background:var(--surface);border:1px solid var(--line);border-radius:14px;padding:20px 22px 12px;margin:16px 0}
.chart-card.hero{border-color:#2c4a3e;box-shadow:0 0 0 1px #14372c inset}
.chart-head{display:flex;justify-content:space-between;align-items:flex-start;gap:16px;flex-wrap:wrap}
.chart-head .sub{color:var(--text2);font-size:13.5px;margin:2px 0 6px;max-width:840px}
.chart svg{width:100%;height:auto;display:block}
.caption{color:var(--muted);font-size:12.5px;margin:6px 0 4px}
.panel[hidden]{display:none}
g.group,circle.mark,g.mark{transition:opacity .15s}
.dim{opacity:.16}
.mark{cursor:default}.mark:hover{filter:brightness(1.22)}
#tooltip{position:fixed;pointer-events:none;background:#0b1318;border:1px solid #2b3c46;border-radius:9px;padding:10px 12px;
font:12.5px/1.5 var(--sans);box-shadow:0 14px 34px #000a;z-index:50;max-width:360px;display:none}
#tooltip .v{font:700 15px/1.3 var(--mono);color:var(--text)}#tooltip .h{color:var(--text2);margin-bottom:4px}
#tooltip .k{color:var(--muted)}
.table-wrap{overflow:auto;max-height:560px;border:1px solid var(--line);border-radius:10px;margin:12px 0}
table{width:100%;border-collapse:collapse;font-size:13px;background:var(--surface)}
th,td{padding:7px 10px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap}
th{position:sticky;top:0;background:var(--surface2);z-index:1}
th button{all:unset;cursor:pointer;font:600 10.5px/1.3 var(--mono);letter-spacing:.06em;text-transform:uppercase;color:var(--muted)}
th[aria-sort=ascending] button:after{content:" ▲"}th[aria-sort=descending] button:after{content:" ▼"}
td{font-variant-numeric:tabular-nums}td.l,th.l{text-align:left}
tr:hover td{background:#13202680}
.up{color:#4cd964}.down{color:var(--critical)}.flat{color:var(--muted)}
.dot{display:inline-block;width:10px;height:10px;border-radius:3px;margin-right:6px;vertical-align:-1px}
.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(460px,1fr));gap:16px}
.kv{display:grid;grid-template-columns:minmax(180px,260px) 1fr;gap:6px 18px;font-size:13.5px}
.kv div:nth-child(odd){color:var(--muted)}
.note{border-left:3px solid var(--accent);background:var(--surface);padding:10px 14px;border-radius:0 8px 8px 0;color:var(--text2);font-size:13.5px}
pre{background:#0b1318;border:1px solid var(--line);border-radius:10px;padding:14px;overflow:auto;font:12.5px/1.55 var(--mono);color:var(--text2)}
footer{margin-top:60px;color:var(--muted);font-size:12.5px;border-top:1px solid var(--line);padding-top:18px}
@media (max-width:720px){.wrap{padding:18px 14px 60px}.grid2{grid-template-columns:1fr}.kv{grid-template-columns:1fr}.filters{position:static}.chart{overflow-x:auto}.chart svg{min-width:760px}}
@media (prefers-reduced-motion:reduce){*{transition:none!important;scroll-behavior:auto!important}}
"""

JS = r"""
(function(){
const arch=document.querySelector('[data-control="arch"]');
const state={db:'city',profile:'random',op:'lookup',family:'all',threads:'all',all:false,
  arch:arch?arch.querySelector('button').dataset.value:''};
const wanted=new URLSearchParams(location.search).get('arch');
if(arch&&[...arch.querySelectorAll('button')].some(b=>b.dataset.value===wanted))state.arch=wanted;
function apply(){
  // Merged report: show one architecture and only the options it measured.
  document.querySelectorAll('[data-arch]').forEach(el=>{el.hidden=el.dataset.arch!==state.arch;});
  document.querySelectorAll('[data-arches]').forEach(b=>{b.hidden=!b.dataset.arches.split(' ').includes(state.arch);});
  document.querySelectorAll('[data-control]').forEach(group=>{
    const key=group.dataset.control;
    const current=[...group.querySelectorAll('button')].find(b=>b.dataset.value===String(state[key]));
    if(current&&current.hidden)state[key]=group.querySelector('button:not([hidden])').dataset.value;
  });
  document.querySelectorAll('[data-panel]').forEach(p=>{
    const d=p.dataset;
    const ok=(!d.db||d.db===state.db)&&(!d.profile||d.profile===state.profile)&&(!d.op||d.op===state.op)&&
      (state.family==='all'||!d.family||d.family===state.family);
    p.hidden=!ok;
  });
  document.querySelectorAll('.group[data-threads],.mark[data-threads]').forEach(g=>{
    g.classList.toggle('dim',state.threads!=='all'&&g.dataset.threads!==state.threads);
  });
  document.querySelectorAll('tr[data-db]').forEach(r=>{
    const d=r.dataset;
    const ok=state.all||((!d.db||d.db===state.db)&&(!d.profile||d.profile===state.profile)&&(!d.op||d.op===state.op)&&
      (state.threads==='all'||!d.threads||d.threads===state.threads)&&(state.family==='all'||!d.family||d.family===state.family));
    r.hidden=!ok;
  });
  document.querySelectorAll('[data-control]').forEach(group=>{
    const key=group.dataset.control;
    group.querySelectorAll('button').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.value===String(state[key]))));
  });
}
document.querySelectorAll('[data-control] button').forEach(b=>b.addEventListener('click',()=>{
  const key=b.closest('[data-control]').dataset.control;state[key]=b.dataset.value;apply();
  if(key==='arch'){const url=new URL(location.href);url.searchParams.set('arch',state.arch);history.replaceState(null,'',url);}
}));
const all=document.getElementById('show-all');
if(all){all.addEventListener('change',()=>{state.all=all.checked;apply();});}
const tip=document.getElementById('tooltip');
function show(el,x,y){
  let rows;try{rows=JSON.parse(el.dataset.tip);}catch(e){return;}
  tip.replaceChildren();
  rows.forEach((r,i)=>{
    if(i===0){const v=document.createElement('div');v.className='v';v.textContent=r[1];
      const h=document.createElement('div');h.className='h';h.textContent=r[0];tip.append(v,h);return;}
    const line=document.createElement('div');const k=document.createElement('span');k.className='k';
    k.textContent=r[0]+': ';const v=document.createElement('span');v.textContent=r[1];line.append(k,v);tip.append(line);
  });
  tip.style.display='block';
  const w=tip.offsetWidth,h=tip.offsetHeight;
  let left=x+16,top=y+16;
  if(left+w>innerWidth-8)left=x-w-16;if(top+h>innerHeight-8)top=y-h-16;
  tip.style.left=Math.max(8,left)+'px';tip.style.top=Math.max(8,top)+'px';
}
function hide(){tip.style.display='none';}
document.addEventListener('pointermove',e=>{const el=e.target.closest&&e.target.closest('[data-tip]');if(el)show(el,e.clientX,e.clientY);else hide();});
document.addEventListener('focusin',e=>{const el=e.target.closest&&e.target.closest('[data-tip]');if(el){const r=el.getBoundingClientRect();show(el,r.right,r.top);}});
document.addEventListener('focusout',hide);
window.addEventListener('scroll',hide,{passive:true});
document.addEventListener('keydown',e=>{if(e.key==='Escape')hide();});
document.querySelectorAll('table.sortable').forEach(table=>{
  table.querySelectorAll('th').forEach((th,index)=>{
    const button=th.querySelector('button');if(!button)return;
    button.addEventListener('click',()=>{
      const dir=th.getAttribute('aria-sort')==='ascending'?'descending':'ascending';
      table.querySelectorAll('th').forEach(h=>h.removeAttribute('aria-sort'));th.setAttribute('aria-sort',dir);
      const body=table.tBodies[0];const rows=[...body.rows];
      const val=r=>{const c=r.cells[index];const v=c.dataset.v!==undefined?c.dataset.v:c.textContent.trim();const n=parseFloat(v);return isNaN(n)?v:n;};
      rows.sort((a,b)=>{const x=val(a),y=val(b);const s=(typeof x==='number'&&typeof y==='number')?x-y:String(x).localeCompare(String(y));return dir==='ascending'?s:-s;});
      rows.forEach(r=>body.append(r));
    });
  });
});
apply();
})();
"""


def seg(key, label, options, default, arches=None):
    """A filter control; arches maps an option to the architectures that have it (merged report)."""
    arches = arches or {}
    buttons = ''.join(f'<button type="button" data-value="{esc(v)}" aria-pressed="{str(v == default).lower()}"'
                      + (f' data-arches="{esc(" ".join(arches[v]))}"' if v in arches else '') + f'>{esc(t)}</button>'
                      for v, t in options)
    return (f'<div class="control"><span id="lbl-{key}">{esc(label)}</span>'
            f'<div class="seg" role="group" aria-labelledby="lbl-{key}" data-control="{key}">{buttons}</div></div>')


def change_cell(value, good_when_positive=True, digits=1):
    if value is None:
        return '<td data-v="">n/a</td>'
    good = value > 0 if good_when_positive else value < 0
    cls = 'up' if good else 'down' if value != 0 else 'flat'
    icon = '▲' if value > 0 else '▼' if value < 0 else '≈'
    return f'<td data-v="{value:.4f}"><span class="{cls}">{icon}</span> {esc(fmt_pct(value, digits))}</td>'


def direction_cell(comp):
    d = comp['direction']
    icon = {'faster': '▲', 'slower': '▼', 'inconclusive': '≈'}[d]
    cls = {'faster': 'up', 'slower': 'down', 'inconclusive': 'flat'}[d]
    return f'<td class="l" data-v="{d}"><span class="{cls}">{icon}</span> {d}</td>'


def throughput_table(ctx):
    head = ['Database', 'Profile', 'Operation', 'Family', 'Queries', 'Threads', 'Hit ratio',
            'Before M/s', 'After M/s', 'Speedup', 'Change', '95% CI', 'Verdict', 'Before CV', 'After CV',
            'Before ms', 'After ms', 'Before CPU %', 'After CPU %', 'Before cycles/op', 'After cycles/op']
    rows = []
    for c in ctx.s['throughput']:
        b, a, comp = c['before'], c['after'], c['comparison']
        cyc = lambda v: f'{c[v]["core_cycles_per_lookup"]["median"]:.0f}' if c[v].get('core_cycles_per_lookup') else 'n/a'
        rows.append(
            f'<tr data-db="{c["database"]}" data-profile="{c["profile"]}" data-op="{c["operation"]}" data-threads="{c["threads"]}" data-family="{c["family"]}">'
            f'<td class="l">{esc(DB_LABEL[c["database"]])}</td><td class="l">{esc(PROFILE_TITLE[c["profile"]])}</td>'
            f'<td class="l">{esc(OP_TITLE[c["operation"]])}</td><td class="l">IPv{c["family"]}</td>'
            f'<td data-v="{c["queries"]}">{SIZE_LABEL[c["queries"]]}</td><td>{c["threads"]}</td>'
            f'<td data-v="{c["hit_ratio"]:.6f}">{c["hit_ratio"] * 100:.2f}%</td>'
            f'<td data-v="{b["throughput_mlps"]["median"]:.6f}">{fmt_mlps(b["throughput_mlps"]["median"])}</td>'
            f'<td data-v="{a["throughput_mlps"]["median"]:.6f}">{fmt_mlps(a["throughput_mlps"]["median"])}</td>'
            f'<td data-v="{comp["speedup"]:.6f}">{fmt_speedup(comp["speedup"])}</td>'
            + change_cell(comp['change_percent']) +
            f'<td data-v="{comp["speedup_ci95_low"]:.6f}">×{comp["speedup_ci95_low"]:.2f}–{comp["speedup_ci95_high"]:.2f}</td>'
            + direction_cell(comp) +
            f'<td data-v="{b["throughput_mlps"]["cv_percent"]:.4f}">{b["throughput_mlps"]["cv_percent"]:.1f}%</td>'
            f'<td data-v="{a["throughput_mlps"]["cv_percent"]:.4f}">{a["throughput_mlps"]["cv_percent"]:.1f}%</td>'
            f'<td data-v="{b["elapsed_ms"]["median"]:.6f}">{fmt_ms(b["elapsed_ms"]["median"])}</td>'
            f'<td data-v="{a["elapsed_ms"]["median"]:.6f}">{fmt_ms(a["elapsed_ms"]["median"])}</td>'
            f'<td>{b["cpu_utilization_percent"]["median"]:.1f}</td><td>{a["cpu_utilization_percent"]["median"]:.1f}</td>'
            f'<td>{cyc("before")}</td><td>{cyc("after")}</td></tr>')
    return table_html('throughput-table', head, rows, 'Throughput results for every configuration')


def latency_table(ctx):
    head = ['Database', 'Profile', 'Operation', 'Family', 'Queries', 'Threads', 'Samples/version',
            'Before p50', 'After p50', 'p50 change', 'Before p95', 'After p95', 'Before p99', 'After p99',
            'p99 change', 'Before max', 'After max', 'Before min', 'After min']
    rows = []
    for c in ctx.s['latency']:
        lb, la = c['before']['latency_pooled_ns'], c['after']['latency_pooled_ns']
        l = c['latency_comparison']
        rows.append(
            f'<tr data-db="{c["database"]}" data-profile="{c["profile"]}" data-op="{c["operation"]}" data-threads="{c["threads"]}" data-family="{c["family"]}">'
            f'<td class="l">{esc(DB_LABEL[c["database"]])}</td><td class="l">{esc(PROFILE_TITLE[c["profile"]])}</td>'
            f'<td class="l">{esc(OP_TITLE[c["operation"]])}</td><td class="l">IPv{c["family"]}</td>'
            f'<td data-v="{c["queries"]}">{SIZE_LABEL[c["queries"]]}</td><td>{c["threads"]}</td>'
            f'<td data-v="{lb["observations"]}">{lb["observations"]:,}</td>'
            f'<td data-v="{lb["p50"]}">{fmt_ns(lb["p50"])}</td><td data-v="{la["p50"]}">{fmt_ns(la["p50"])}</td>'
            + change_cell(-l['p50']['reduction_percent'] if l['p50']['reduction_percent'] is not None else None, False) +
            f'<td data-v="{lb["p95"]}">{fmt_ns(lb["p95"])}</td><td data-v="{la["p95"]}">{fmt_ns(la["p95"])}</td>'
            f'<td data-v="{lb["p99"]}">{fmt_ns(lb["p99"])}</td><td data-v="{la["p99"]}">{fmt_ns(la["p99"])}</td>'
            + change_cell(-l['p99']['reduction_percent'] if l['p99']['reduction_percent'] is not None else None, False) +
            f'<td data-v="{lb["max"]}">{fmt_ns(lb["max"])}</td><td data-v="{la["max"]}">{fmt_ns(la["max"])}</td>'
            f'<td data-v="{lb["min"]}">{fmt_ns(lb["min"])}</td><td data-v="{la["min"]}">{fmt_ns(la["min"])}</td></tr>')
    return table_html('latency-table', head, rows, 'Pooled sampled latency for every configuration')


def memory_table(ctx):
    head = ['Database', 'Family', 'Queries', 'Threads', 'Corpus MiB', 'Before RSS open', 'After RSS open',
            'Before heap open', 'After heap open', 'Before mapping open', 'After mapping open',
            'Before RSS warm-up', 'After RSS warm-up', 'Before peak', 'After peak', 'Peak change']
    rows = []
    for m in ctx.s['memory']:
        b, a = m['before'], m['after']
        med = lambda d, k: d[k]['median']
        peak = m['comparison']['peak_rss_lookups_mib']
        rows.append(
            f'<tr data-db="{m["database"]}" data-threads="{m["threads"]}" data-family="{m["family"]}">'
            f'<td class="l">{esc(DB_LABEL[m["database"]])}</td><td class="l">IPv{m["family"]}</td>'
            f'<td data-v="{m["queries"]}">{SIZE_LABEL[m["queries"]]}</td><td>{m["threads"]}</td>'
            f'<td>{b["corpus_mib"]:.1f}</td>'
            + ''.join(f'<td data-v="{med(x, k):.4f}">{med(x, k):,.1f}</td>'
                      for k in ('rss_after_open_mib', 'heap_after_open_mib', 'mapping_rss_after_open_mib',
                                'rss_after_warmup_mib', 'peak_rss_lookups_mib') for x in (b, a))
            + change_cell(peak['change_percent'], False) + '</tr>')
    return table_html('memory-table', head, rows, 'Memory per configuration (MiB, medians of fresh processes)')


def startup_table(ctx):
    head = ['Database', 'Version', 'Open variant', 'Page cache', 'Runs', 'Open median', 'Open p25–p75',
            'Context creation', 'Heap after open', 'RSS after open', 'Mapping resident']
    rows = []
    variant_text = {'default': 'MMDB_open (default flags)', 'no-prepare': 'MMDB_open with MMDB_NO_PREPARE',
                    'context': 'MMDB_NO_PREPARE + MMDB_create_lookup_context'}
    for s in ctx.s['startup']:
        o = s['open_ms']
        rows.append(
            f'<tr data-db="{s["database"]}"><td class="l">{esc(DB_LABEL[s["database"]])}</td>'
            f'<td class="l"><span class="dot" style="background:var(--{s["version"]})"></span>{C.VERSION_LABEL[s["version"]]}</td>'
            f'<td class="l">{esc(variant_text[s["variant"]])}</td><td class="l">{"cold" if s["cold"] else "warm"}</td>'
            f'<td>{o["n"]}</td><td data-v="{o["median"]:.6f}">{fmt_ms(o["median"])}</td>'
            f'<td>{fmt_ms(o["q1"])} – {fmt_ms(o["q3"])}</td>'
            f'<td>{fmt_ms(s["context_ms"]["median"]) if s["context_ms"] else "—"}</td>'
            f'<td>{fmt_mib(s["heap_after_open_mib"]["median"])}</td><td>{fmt_mib(s["rss_after_open_mib"]["median"])}</td>'
            f'<td>{fmt_mib(s["mapping_rss_mib"]["median"])}</td></tr>')
    return table_html('startup-table', head, rows, 'Database opening and preparation')


def allocation_table(ctx):
    head = ['Database', 'Family', 'Profile', 'Operation', 'Version', 'Allocations/lookup', 'Bytes/lookup',
            'Allocations/hit', 'Open allocations', 'Open bytes']
    rows = []
    for r in ctx.s['allocations']:
        rows.append(
            f'<tr data-db="{r["database"]}" data-profile="{r["profile"]}" data-op="{r["operation"]}" data-family="{r["family"]}">'
            f'<td class="l">{esc(DB_LABEL[r["database"]])}</td><td class="l">IPv{r["family"]}</td>'
            f'<td class="l">{esc(PROFILE_TITLE[r["profile"]])}</td><td class="l">{esc(OP_TITLE[r["operation"]])}</td>'
            f'<td class="l"><span class="dot" style="background:var(--{r["version"]})"></span>{C.VERSION_LABEL[r["version"]]}</td>'
            f'<td>{r["allocations_per_lookup"]:.4f}</td><td>{r["bytes_per_lookup"]:.2f}</td>'
            f'<td>{(r["allocations_per_hit"] or 0):.4f}</td><td>{r["open_alloc_calls"]}</td>'
            f'<td data-v="{r["open_alloc_bytes"]}">{r["open_alloc_bytes"]:,}</td></tr>')
    return table_html('allocation-table', head, rows, 'Allocation counts from instrumented binaries')


def workload_table(ctx):
    head = ['Database', 'Family', 'Profile', 'Queries', 'Hits', 'Misses', 'Hit ratio', 'Field present',
            'Populated leaves', 'Sampled networks', 'Corpus SHA-256', 'Result digest identical']
    rows = []
    for key in sorted(ctx.s['workloads']):
        w = ctx.s['workloads'][key]
        if 'hits' not in w:
            continue
        rows.append(
            f'<tr data-db="{w["database"]}" data-profile="{w["profile"]}"><td class="l">{esc(DB_LABEL[w["database"]])}</td>'
            f'<td class="l">IPv{w["family"]}</td><td class="l">{esc(PROFILE_TITLE[w["profile"]])}</td>'
            f'<td data-v="{w["queries"]}">{SIZE_LABEL.get(w["queries"], w["queries"])}</td>'
            f'<td>{w["hits"]:,}</td><td>{w["misses"]:,}</td><td>{w["hit_ratio"] * 100:.3f}%</td>'
            f'<td>{w.get("field_present", 0):,}</td><td>{(w.get("populated_leaves") or 0):,}</td>'
            f'<td>{(w.get("sampled_networks") or 0):,}</td><td class="l"><code>{w["corpus_sha256"]}</code></td>'
            f'<td class="l">{"yes" if w.get("differential_identical") else "NO"}</td></tr>')
    return table_html('workload-table', head, rows, 'Query corpora and differential checks')


def table_html(table_id, head, rows, label):
    ths = ''.join(f'<th scope="col" class="{"l" if i < 4 else ""}"><button type="button">{esc(h)}</button></th>'
                  for i, h in enumerate(head))
    return (f'<div class="table-wrap"><table class="sortable" id="{table_id}" aria-label="{esc(label)}">'
            f'<thead><tr>{ths}</tr></thead><tbody>{"".join(rows)}</tbody></table></div>')


def chart_panel(svg, title, subtitle, attrs, caption=None, hero=False):
    data = ''.join(f' data-{k}="{esc(v)}"' for k, v in attrs.items())
    cap = f'<p class="caption">{esc(caption)}</p>' if caption else ''
    return (f'<div class="panel chart-card{" hero" if hero else ""}" data-panel{data}>'
            f'<div class="chart-head"><div><h3>{esc(title)}</h3><p class="sub">{esc(subtitle)}</p></div></div>'
            f'<div class="chart">{svg}</div>{cap}</div>')


def throughput_section(ctx, size, hero=True):
    out = []
    for family in FAMILIES:
        for db in ('city', 'synthetic'):
            for profile in ('random', 'hits'):
                for op in ('lookup', 'decode'):
                    configs = ctx.select('throughput', database=db, profile=profile, operation=op, family=family, queries=size)
                    if not configs:
                        continue
                    svg = build_throughput(ctx, db, profile, op, family, size, False)
                    title = throughput_title(family, profile, op, size)
                    subtitle = (f'{DB_LABEL[db]} · {PROFILE_TEXT[profile]} ({fmt_hits(configs[0]["hit_ratio"])} hits) · {ctx.operation_text(db, op)} · '
                                f'{size:,} queries in total, split across {joined(THREADS)} pinned workers · median of {ctx.reps} runs, '
                                '95% bootstrap CI')
                    out.append(chart_panel(svg, title, subtitle, {'db': db, 'profile': profile, 'op': op, 'family': family},
                                           hero=hero))
    return ''.join(out)


def candlestick_section(ctx, family):
    out = []
    for db in ('city', 'synthetic'):
        for profile in ('random', 'hits'):
            for op in ('lookup', 'decode'):
                if not ctx.select('latency', database=db, profile=profile, operation=op, family=family, queries=SIZE):
                    continue
                svg = build_candlestick(ctx, db, profile, op, family, SIZE, False)
                title = candlestick_title(family, op) + f' ({SIZE_LABEL[SIZE]})'
                hits = ctx.one('latency', database=db, profile=profile, operation=op, family=family,
                               queries=SIZE, threads=1)['hit_ratio']
                subtitle = (f'{DB_LABEL[db]} · {PROFILE_TEXT[profile]} ({fmt_hits(hits)} hits) · {ctx.operation_text(db, op)} · '
                            f'{SIZE:,} queries in total · pooled samples of {ctx.reps} repetitions per bar')
                out.append(chart_panel(svg, title, subtitle,
                                       {'db': db, 'profile': profile, 'op': op, 'family': family,
                                        'chart': 'candlestick'}))
    return ''.join(out)


def headline_cards(ctx):
    h = ctx.s['headline']['throughput']
    city, syn = h['city'], h.get('synthetic')
    crl = h['city_random_lookup']
    memory = next(m for m in ctx.s['memory'] if m['database'] == 'city' and m['family'] == 4
                  and m['queries'] == SIZE and m['threads'] == 1)
    start = {(r['version'], r['variant']): r for r in ctx.s['startup'] if r['database'] == 'city' and not r['cold']}
    lat = ctx.s['headline']['latency']['city']
    cards = [
        ('GeoLite2-City throughput', fmt_speedup(city['geomean_speedup']),
         f'{fmt_pct(city["geomean_change_percent"])} geometric mean of {city["configurations"]} configurations · '
         f'95% CI ×{city["geomean_ci95_low"]:.2f}–{city["geomean_ci95_high"]:.2f} · {city["faster"]} faster, '
         f'{city["slower"]} slower, {city["inconclusive"]} inconclusive'),
        ('Random lookups, GeoLite2-City', fmt_speedup(crl['geomean_speedup']),
         f'{crl["configurations"]} configurations (IPv4/IPv6 × {"/".join(str(t) for t in THREADS)} threads, {SIZE_LABEL[SIZE]} queries) · range '
         f'{fmt_speedup(crl["min_speedup"])}–{fmt_speedup(crl["max_speedup"])}'),
    ] + ([
        ('Synthetic fixture throughput', fmt_speedup(syn['geomean_speedup']),
         f'{fmt_pct(syn["geomean_change_percent"])} geometric mean of {syn["configurations"]} configurations · '
         f'{syn["faster"]} faster, {syn["slower"]} slower, {syn["inconclusive"]} inconclusive'),
    ] if syn else []) + [
        ('p99 latency, GeoLite2-City', f'{lat["p99"]["improved"]}/{lat["configurations"]}',
         f'configurations with a lower pooled p99 · geometric-mean p99 ratio ×{lat["p99"]["geomean_ratio"]:.2f} · '
         f'{lat["p99"]["worse"]} higher'),
        ('Open + preparation cost', fmt_ms(start[('after', 'default')]['open_ms']['median']),
         f'GeoLite2-City MMDB_open, warm cache (Before {fmt_ms(start[("before", "default")]["open_ms"]["median"])})'),
        ('Additional reader memory', f'+{memory["comparison"]["heap_after_open_mib"]["delta"]:.1f} MiB',
         f'GeoLite2-City heap after open · process RSS after open {fmt_mib(memory["before"]["rss_after_open_mib"]["median"])} → '
         f'{fmt_mib(memory["after"]["rss_after_open_mib"]["median"])}'),
    ]
    return '<div class="cards">' + ''.join(
        f'<div class="card"><div class="label">{esc(a)}</div><div class="value">{esc(b)}</div><div class="detail">{esc(c)}</div></div>'
        for a, b, c in cards) + '</div>'


def environment_block(ctx):
    env = ctx.env
    cpu = env['cpu']
    host = ctx.s['host_activity'] or {}
    build = ctx.s['build']
    busiest = max(ctx.s['throughput'], key=lambda c: c['threads'])
    smt = ('SMT active' if cpu['smt_active'] == '1'
           else 'SMT inactive' if (cpu.get('threads_per_core') or 1) > 1 else 'no SMT')
    first = cpu['topology'][0] if cpu.get('topology') else {}
    policy = ([f'amd-pstate {cpu["amd_pstate"]}'] if cpu.get('amd_pstate') else []) + \
             ([f'governor {ctx.governor}'] if ctx.governor else []) + \
             ([f'EPP {first["energy_performance_preference"]}'] if first.get('energy_performance_preference') else []) + \
             ([f'boost {"on" if cpu["boost"] == "1" else "off"}'] if cpu.get('boost') is not None else []) + \
             ([f'max {float(cpu["max_mhz"]):.0f} MHz'] if cpu.get('max_mhz') else [])
    target = env['toolchain'].get('gcc_target') or (
        {'arch': 'x86-64', 'tune': 'generic'} if env['os']['architecture'] == 'x86_64' else {})
    target = ' '.join(f'-m{k}={target[k]}' for k in ('arch', 'cpu', 'tune') if target.get(k)) or ctx.arch
    paranoid = env['os']['perf_event_paranoid']
    perf = 'perf unavailable' if paranoid and int(paranoid) > 2 else 'perf not used'
    counters = ('core cycles read with AMD RDPRU (APERF)' if ctx.cycles
                else 'no cycle counter readable from user space, so no cycles per lookup')
    ci = ctx.ci or {}
    rows = [
        ('CPU model', cpu['model']),
        ('Architecture', f'{ctx.arch} ({env["toolchain"].get("gcc_target", {}).get("machine") or env["os"]["architecture"]})'),
        ('Topology', f'{plural(cpu["sockets"], "socket")} · {cpu["physical_cores"]} physical cores · {cpu["logical_cpus"]} logical CPUs · '
                     f'{smt}'),
        ('CPUs available to the benchmark', f'{cpu["available_to_benchmark"]} (affinity {cpu["affinity"][0]}–{cpu["affinity"][-1]}; '
                                            f'cgroup CPU quota: {env["os"]["cgroup"]["cpu.max"]})'),
        ('Worker placement', f'{"first SMT sibling of each physical core" if ctx.smt else "one logical CPU per physical core"}, '
                             f'{"ordered by ACPI CPPC rank" if ctx.ranked else "in CPU number order"}: '
                             f'CPUs {", ".join(str(c) for c in busiest["worker_cpus"])} — '
                             f'{"oversubscribed" if any(c["oversubscribed"] for c in ctx.s["throughput"]) else "never oversubscribed"}'),
        ('Caches', ', '.join(f'{k} {v}' for k, v in cpu['caches'].items() if v) or 'not reported'),
        ('Instruction sets (relevant)', ' '.join(cpu['instruction_sets']) or 'none of the recorded ones'),
        ('Frequency policy', ', '.join(policy) or 'not visible from this machine (no cpufreq interface)'),
        ('Memory', f'{env["memory"]["MemTotal_kib"] / 1024 / 1024:.1f} GiB RAM · {env["memory"]["MemAvailable_kib"] / 1024 / 1024:.1f} GiB available at start · '
                   f'THP {env["transparent_hugepage"]["enabled"]}'),
        ('OS / kernel', f'{env["os"]["pretty_name"]} · Linux {env["os"]["kernel"]} · clocksource {env["os"]["clocksource"]} · '
                        f'virtualization: {env["os"]["virtualization"]}'),
        ('Compilers', f'{env["toolchain"]["gcc"]} · {env["toolchain"]["cmake"]} · ninja {env["toolchain"]["ninja"]} · {env["toolchain"]["glibc"]}'),
        ('Library build', build['library_compile_commands'].get('after', '') + f'  (GCC default target: {target}; no ISA-specific flags)'),
        ('Hardware counters', f'perf_event_paranoid={paranoid}: {perf}; {counters}'),
    ] + ([
        ('Runner', f'{ci.get("service")} {ci.get("runner_environment") or ""} runner {ci.get("runner_name") or ""} · '
                   f'image {ci.get("imageos") or "?"} {ci.get("imageversion") or ""} · run {ci.get("github_run_id")} · '
                   f'{ci.get("github_repository")}@{(ci.get("github_sha") or "")[:7]}'),
    ] if ctx.ci else []) + [
        ('Host activity', f'{ctx.host()}; system CPU busy median {host.get("system_cpu_busy_median", 0) * 100:.1f}% '
                          f'(p90 {host.get("system_cpu_busy_p90", 0) * 100:.1f}%), load average median {host.get("loadavg1_median", 0):.2f}'),
    ]
    return '<div class="kv">' + ''.join(f'<div>{esc(k)}</div><div>{esc(v)}</div>' for k, v in rows) + '</div>'


def filters_html(ctxs):
    """The sticky filter bar. With several architectures (merged report) an
    Architecture control comes first and every option lists the architectures
    that measured it, so the page only offers what the shown one has."""
    merged = len(ctxs) > 1

    def measured(field, options):
        found = []
        for value, text in options:
            have = [c.key for c in ctxs if any(str(x[field]) == str(value) for x in c.s['throughput'])]
            if have:
                found.append((value, text, have))
        return found

    def measured_seg(key, field, label, options):
        found = measured(field, options)
        return (seg(key, label, [(v, t) for v, t, _ in found], found[0][0],
                    {v: have for v, _, have in found} if merged else None) if len(found) > 1 else '')
    counts = sorted({x['threads'] for c in ctxs for x in c.s['throughput']})
    threads = measured('threads', [(t, str(t)) for t in counts])
    return ('<nav class="filters" aria-label="Report filters">'
            + (seg('arch', 'Architecture', [(c.key, c.arch) for c in ctxs], ctxs[0].key) if merged else '')
            + measured_seg('db', 'database', 'Database', [('city', 'GeoLite2-City'), ('synthetic', 'Synthetic')])
            + measured_seg('profile', 'profile', 'Workload profile', [('random', 'Random'), ('hits', 'Populated networks')])
            + measured_seg('op', 'operation', 'Operation', [('lookup', 'Lookup'), ('decode', 'Lookup + decode')])
            + seg('family', 'IP family', [('all', 'Both'), ('4', 'IPv4'), ('6', 'IPv6')], 'all')
            + seg('threads', 'Highlight threads', [('all', 'All')] + [(str(t), str(t)) for t, _, _ in threads], 'all',
                  {str(t): have for t, _, have in threads} if merged else None)
            + '<label class="control"><span>Tables</span><span style="font:13px var(--sans);text-transform:none;letter-spacing:0;color:var(--text2)">'
            '<input type="checkbox" id="show-all"> show every configuration</span></label></nav>')


def document(title, body):
    return (f'<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{esc(title)}</title><style>{CSS}</style></head><body><div class="wrap">'
            + body + '</div><div id="tooltip" role="tooltip"></div>'
            f'<script>{JS}</script></body></html>\n')


def html_report(ctx, manifest):
    top, rest = report_sections(ctx, manifest)
    return document(f'libmaxminddb Before/After Performance — {ctx.cpu}', ''.join(top) + filters_html([ctx]) + ''.join(rest))


def report_sections(ctx, manifest):
    """The report above the filter bar (header, cards, note) and below it."""
    s = ctx.s
    v = s['validation']
    proto = s['protocol']
    memory_panels = ''.join(chart_panel(build_memory(ctx, False), 'Memory Usage — Reader State and Process RSS',
                                        f'Fresh process per measurement, median of {ctx.memory_iterations} · heap from mallinfo2 · RSS is whole-process',
                                        {}, caption_for(ctx, {'kind': 'memory'})) for _ in [0])
    sections = [
        f'<header><div class="eyebrow">Benchmark lab / Before–After comparison</div>'
        f'<h1>libmaxminddb lookups, <span class="accent">measured</span> <br>on {esc(ctx.cpu)}</h1>'
        f'<p class="lead">Before <code>{s["before"]["commit"]}</code> (upstream main) versus After '
        f'<code>{s["after"]["commit"]}</code> (three commits: specialized packed traversal, allocation-free short '
        'MMDB_get_value() paths, then automatic radix-16 preparation with aligned native child records). Same harness source, identical compiler '
        f'flags, static libraries, {v["measured_runs"]:,} measured runs and {v["measured_lookups"] / 1e9:.2f} billion measured lookups.</p>'
        f'<div class="meta"><span><b>CPU</b> {esc(ctx.cpu)}</span><span><b>Architecture</b> {esc(ctx.arch)}</span>'
        f'<span><b>Cores</b> {ctx.cores} / {ctx.logical} threads</span>'
        f'<span><b>OS</b> {esc(ctx.os)} · Linux {esc(ctx.kernel)}</span><span><b>Compiler</b> GCC {esc(ctx.gcc)} -O3 -DNDEBUG</span>'
        f'<span><b>Repetitions</b> {ctx.reps} per configuration, alternating order</span>'
        f'<span><b>Recorded</b> {esc(s["environment"]["recorded"])}</span></div></header>',
        headline_cards(ctx),
        f'<p class="note">All values come from <code>{ctx.prefix}performance-comparison-data/summary.json</code>. Speedup = After / Before '
        'median throughput; change = (speedup − 1) × 100; latency reduction = (1 − After / Before) × 100. '
        'Hover or focus any bar, candle or cell for exact values; every value is also in the tables below.</p>',
        None,
        f'<h2 id="throughput">Throughput at {SIZE_LABEL[SIZE]} queries</h2><p class="lead">Total work is fixed: '
        + ', '.join(f'1 worker performs {SIZE:,} lookups' if t == 1 else f'{t} workers {SIZE // t:,} each' for t in THREADS)
        + '.</p>',
        throughput_section(ctx, SIZE),
        '<h2 id="latency">Latency distribution</h2><p class="lead">Separate latency campaign: one actual operation in '
        f'every {proto["latency_sampling_stride"]} is timed individually; workers are merged and repetitions pooled before '
        'percentiles are computed. Lower whisker min → p50, body p50 → p95, upper whisker p95 → p99, ◆ p50; the maximum is listed '
        'at the right of each row and in tooltips.</p>',
        chart_card_group('IPv4', candlestick_section(ctx, 4)),
        chart_card_group('IPv6', candlestick_section(ctx, 6)),
        '<h2 id="scaling">Thread scaling</h2><p class="lead">Scaling = throughput(N) / throughput(1) of the same version; '
        'parallel efficiency = scaling / N × 100.'
        + (' Configurations that scale above 100% do so because the measured core cycles per lookup of each worker drop '
           'as workers are added (see the chart footers); the cause was not isolated.' if ctx.superlinear() else '')
        + '</p>',
        ''.join(chart_panel(build_scaling(ctx, db, profile, op, False), f'Thread Scaling — {PROFILE_TITLE[profile]} {OP_TITLE[op]}',
                            f'{DB_LABEL[db]} · {ctx.operation_text(db, op)}', {'db': db, 'profile': profile, 'op': op})
                for db in DATABASES for profile in PROFILES for op in OPERATIONS
                if ctx.select('throughput', database=db, profile=profile, operation=op)),
        '<h2 id="memory">Memory</h2><p class="lead">Measured in fresh processes with the same preparation and warm-up as the '
        'throughput campaign. Process RSS is a whole-process metric; the library heap (mallinfo2 in-use bytes after '
        '<code>MMDB_open</code>) isolates reader and index allocations.</p>',
        memory_panels,
        '<h2 id="startup">Startup and index preparation</h2><p class="lead">After builds its native child array and 65,536-entry '
        'root tables inside <code>MMDB_open</code>; <code>MMDB_NO_PREPARE</code> skips that work. Opening time is measured '
        'separately from steady-state lookups.</p>',
        chart_panel(build_startup(ctx, False), 'Startup — Database Opening and Index Preparation',
                    'Fresh process per open, pinned to one core · warm and cold page cache', {},
                    caption_for(ctx, {'kind': 'startup'})),
        '<h2 id="allocations">Allocations</h2>',
        chart_panel(build_allocations(ctx, False), 'Heap Allocations per Lookup + Field Decode',
                    'Instrumented binaries only · measured lookup phase', {}, caption_for(ctx, {'kind': 'allocations'})),
        '<h2 id="matrix">Every throughput configuration</h2>',
        ''.join(chart_panel(build_matrix(ctx, db, False), f'Throughput Speedup Matrix — {DB_LABEL[db]}',
                            'Rows: family × profile × operation · columns: worker threads', {'db': db})
                for db in ctx.timed_dbs),
        attribution_html(ctx),
        '<h2 id="tables">Result tables</h2><p class="lead">Rows follow the filters above unless “show every configuration” is '
        'checked. Click a column header to sort.</p>',
        '<h3>Throughput</h3>', throughput_table(ctx),
        '<h3>Latency (pooled sampled percentiles)</h3>', latency_table(ctx),
        '<h3>Memory</h3>', memory_table(ctx),
        '<h3>Startup</h3>', startup_table(ctx),
        '<h3>Allocations</h3>', allocation_table(ctx),
        '<h2 id="hardware">Hardware and methodology</h2>', environment_block(ctx),
        methodology_html(ctx),
        '<h2 id="workloads">Workloads and correctness</h2>', workload_table(ctx),
        f'<p class="note">Validation: checksums identical across versions and repetitions: {v["all_checksums_identical"]} · '
        f'completed counts and zero errors: {v["all_completed_ok"]} · corpora identical between versions: {v["all_corpora_consistent"]} · '
        f'ordered per-query differential digests identical on {v["differential_corpora"]} corpora: {v["differential_identical"]}.</p>',
        limitations_html(ctx),
        '<h2 id="reproduce">Reproduction</h2>', f'<pre>{esc(reproduce(ctx))}</pre>',
        '<h2 id="figures">Exported figures</h2>' + figures_list_html(manifest),
        f'<footer>Generated {esc(datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"))} by '
        '<code>benchmarks/reporting/report.py</code> from saved results. Measured on '
        f'{esc(ctx.cpu)}. Before {s["before"]["commit"]} · After {s["after"]["commit"]}.</footer>',
    ]
    split = sections.index(None)
    return sections[:split], sections[split + 1:]


# Preferred order of the architectures in the merged report.
ARCH_ORDER = ('x86_64', 'aarch64')


def merged_report(site):
    """One page with the reports of every architecture directory in site,
    switched by the Architecture filter. Figures stay in each directory."""
    directories = sorted((p.parent.parent for p in Path(site).glob('*/performance-comparison-data/summary.json')),
                         key=lambda d: (ARCH_ORDER.index(d.name) if d.name in ARCH_ORDER else len(ARCH_ORDER), d.name))
    if len(directories) < 2:
        raise SystemExit(f'{site}: a merged report needs at least two architecture directories')
    ctxs, tops, rests = [], [], []
    for index, directory in enumerate(directories):
        summary = json.loads((directory / 'performance-comparison-data/summary.json').read_text())
        manifest = json.loads((directory / 'performance-comparison-assets/manifest.json').read_text())
        configure(summary)
        ctx = Context(summary, key=directory.name, prefix=f'{directory.name}/')
        top, rest = report_sections(ctx, [{**m, 'png': ctx.prefix + m['png'], 'svg': ctx.prefix + m['svg']}
                                          for m in manifest])
        top, rest = ''.join(top), ''.join(rest)
        if index:
            # Element ids stay unique: those of the first architecture are kept.
            top, rest = (re.sub(r'\bid="([^"]+)"', rf'id="{ctx.key}-\1"', part) for part in (top, rest))
        hidden = ' hidden' if index else ''
        tops.append(f'<div data-arch="{esc(ctx.key)}"{hidden}>{top}</div>')
        rests.append(f'<div data-arch="{esc(ctx.key)}"{hidden}>{rest}</div>')
        ctxs.append(ctx)
    title = 'libmaxminddb Before/After Performance — ' + joined(f'{c.cpu} ({c.arch})' for c in ctxs)
    return document(title, ''.join(tops) + filters_html(ctxs) + ''.join(rests))


def attribution_html(ctx):
    rows = attribution_rows(ctx)
    if not rows:
        return ''
    body = []
    for a, row in zip(ctx.s['attribution'], rows):
        cells = ''.join(f'<td class="{"l" if i < 3 else ""}">{esc(v)}</td>' for i, v in enumerate(row))
        body.append(f'<tr data-db="{a["database"]}" data-profile="{a["profile"]}" data-threads="{a["threads"]}" data-family="{a["family"]}">{cells}</tr>')
    return ('<h2 id="attribution">Attribution between the traversal changes</h2><p class="lead">A separate three-way '
            'campaign isolates the specialized packed traversal (After opened with <code>MMDB_NO_PREPARE</code>) from the '
            f'automatic radix-16/native preparation, on identical corpora with {ctx.reps} repetitions per variant in rotating '
            'order. Ratios are medians with 95% bootstrap intervals.</p>'
            + table_html('attribution-table', ATTRIBUTION_HEAD, body, 'Attribution between the traversal changes'))


def chart_card_group(label, inner):
    return f'<section aria-label="{esc(label)} latency">{inner}</section>'


def figures_list_html(manifest):
    items = ''.join(f'<li><a href="{esc(m["png"])}">{esc(m["png"].split("/")[-1])}</a> · '
                    f'<a href="{esc(m["svg"])}">SVG</a> — {esc(m["title"])}</li>' for m in manifest)
    return f'<ul>{items}</ul>'


def methodology_html(ctx):
    s = ctx.s
    proto = s['protocol']
    timer = proto['timer_ns']
    after = s['after']
    rebuilt = (f' The After executables were built from {after["built_from"][:7]}, an earlier commit with the identical '
               f'source tree; building {ctx.after_short} gives byte-identical libraries and executables.'
               if after['built_from'] != after['commit'] else '')
    items = [
        ('Versions', f'Before {s["before"]["commit"]} and After {after["commit"]}, exported with git archive into '
                     f'isolated source trees (pinned submodules copied and verified).{rebuilt} Static libraries built with CMake '
                     'Release (-O3 -DNDEBUG) by the same GCC; the same bench.c links each archive. ldd shows no '
                     'libmaxminddb shared object; symbol checks confirm which revision each binary contains.'),
        ('Operations', 'lookup = MMDB_lookup_sockaddr on pre-built sockaddr structures (no text parsing in the timed loop). '
                       'decode = lookup plus MMDB_get_value on hits (country → iso_code on GeoLite2-City, id on the synthetic '
                       'fixture). Records without the field count as absent, identically in both versions.'),
        ('Workloads', 'random: SplitMix64 (seed 0x6a09e667f3bcc909) over the whole IPv4 or IPv6 space. populated networks: '
                      'every reachable data leaf is enumerated with MMDB_read_node, uniformly reservoir-sampled '
                      '(2^20 networks), host bits randomized; IPv6 enumeration skips the IPv4 subtree and its aliases. '
                      'Identical corpora (SHA-256 recorded) for both versions.'),
        ('Throughput protocol', f'{ctx.reps} measured repetitions per configuration; one prepared process per version, '
                                'triggered one repetition at a time, order alternating (even pairs Before first). '
                                f'Each repetition warms up with {proto["warmup_queries_per_repetition"]:,} queries in total, '
                                'then workers pass a spin start gate. Throughput = completed '
                                'lookups / wall-clock time from gate release to the last worker finishing. Thread-local '
                                'checksums, no shared counters, no per-query timers.'),
        ('Latency protocol', f'Separate campaign. Every {proto["latency_sampling_stride"]}th operation is timed with two '
                             f'CLOCK_MONOTONIC_RAW reads (back-to-back pair: min {timer["min"]} ns, median {timer["p50"]} ns, '
                             f'p99 {timer["p99"]} ns; included, not subtracted). Exact integer-nanosecond histograms from all '
                             'workers and repetitions are merged before nearest-rank percentiles floor((n − 1) · p).'),
        ('Statistics', f'Medians with 95% percentile bootstrap intervals ({proto["bootstrap_iterations"]:,} resamples, seed '
                       f'{proto["bootstrap_seed"]:#x}). A difference is conclusive only when the interval of the ratio of '
                       'medians excludes 1. Geometric means weight every configuration equally.'),
        ('Memory and startup', f'Fresh processes: {ctx.memory_iterations} per memory configuration (several at once, each '
                               f'pinned to its own core; nothing is timed) and {ctx.opens[False]} warm and {ctx.opens[True]} '
                               'cold opens per startup variant (one at a time, pinned to one core, rotating order). '
                               'RSS stages read from /proc/self/status; the lookup-phase '
                               'peak resets VmHWM through /proc/self/clear_refs after warm-up. Heap = mallinfo2 in-use bytes; '
                               'database mapping residency from /proc/self/smaps.'),
        ('Counters', 'CPU utilization = thread CPU time (RUSAGE_THREAD) / (wall time × workers). '
                     + ('Core cycles per lookup = APERF delta via RDPRU on each pinned worker CPU / completed lookups. '
                        if ctx.cycles else 'No cycle counter is readable from user space on this CPU, so cycles per '
                        'lookup are not reported. ')
                     + f'Cache-miss and branch-miss counters are unavailable (perf_event_paranoid = '
                       f'{ctx.env["os"]["perf_event_paranoid"]}).'),
    ]
    return '<div class="grid2" style="margin-top:18px">' + ''.join(
        f'<div class="card"><div class="label">{esc(a)}</div><div class="detail" style="font-size:13.5px;margin-top:6px">{esc(b)}</div></div>'
        for a, b in items) + '</div>'


def limitations_html(ctx):
    return '<h2 id="limitations">Limitations</h2><ul>' + ''.join(f'<li>{esc(x)}</li>' for x in limitations(ctx)) + '</ul>'


def limitations(ctx):
    host = ctx.s['host_activity'] or {}
    top = ', '.join(f'{p["name"]} ({p["cpu_percent"]:.0f}%)' for p in host.get('top_processes_mean_cpu_percent', [])[:5])
    untimed = ctx.s['allocations']
    left_out = ([DB_LABEL[d] for d in present(untimed, 'database', DATABASES) if d not in ctx.timed_dbs]
                + [f'{PROFILE_NAME[p]} workloads' for p in present(untimed, 'profile', PROFILES) if p not in ctx.timed_profiles])
    coverage = (f'Throughput covers {ctx.scope()} on {joined(DB_LABEL[d] for d in ctx.timed_dbs)}; latency covers '
                f'{ctx.scope("latency")}, with {ctx.reps} repetitions per version and configuration.'
                + (f' The {joined(left_out)} only enter the differential, memory, startup and allocation measurements.'
                   if left_out else ''))
    if ctx.ci:
        host_text = ('GitHub-hosted runner: a virtual machine on shared cloud hardware, not an isolated benchmark host. '
                     'Other tenants of the physical host and the runner\'s own processes kept running (largest mean '
                     f'consumers: {top}), and the guest\'s core topology need not match the physical one. ')
    elif ctx.host() != 'shared desktop':
        host_text = (f'{ctx.host()[0].upper()}{ctx.host()[1:]}, not an isolated benchmark host: other processes kept '
                     f'running (largest mean consumers: {top}). ')
    else:
        host_text = f'Shared desktop, not an isolated benchmark host: other processes kept running (largest mean consumers: {top}). '
    enabled = (['Turbo boost'] if ctx.env['cpu'].get('boost') == '1' else []) + (['SMT'] if ctx.smt else [])
    frequency = (f'{" and ".join(enabled)} {"stay" if len(enabled) > 1 else "stays"} enabled'
                 + (f' (governor {ctx.governor}).' if ctx.governor else '.') if enabled else '')
    if not ctx.governor:
        frequency += ' CPU frequency is not visible from this machine (no cpufreq interface).'
    frequency += (' Single-thread results run on the highest-ranked core.' if ctx.ranked
                  else ' Single-thread results run on the first allowed CPU.')
    stride = ctx.s['protocol']['latency_sampling_stride']
    other = {'x86-64': 'AArch64', 'AArch64': 'x86-64'}.get(ctx.arch)
    grid = ctx.s['protocol'].get('timer_granularity') or {}
    clock = CLOCK_LABEL.get(ctx.env['os']['clocksource'], f'{ctx.env["os"]["clocksource"]} clock')
    return [
        coverage,
        host_text + f'Alternating order, per-repetition warm-up and {ctx.reps} repetitions limit, but do not remove, their effect; '
        f'{ctx.max_threads}-worker runs occupy {"every physical core" if ctx.max_threads >= ctx.cores else ctx.busiest()} '
        'and are the most exposed.',
        frequency.strip(),
        f'Latency percentiles describe sampled operations (one in {"sixteen" if stride == 16 else stride}) and include the timer-read overhead; '
        'the maximum is the largest sampled value, not of every lookup.',
        f'GeoLite2-City is a real database (build {ctx.databases["city"]["build_date"]}) that cannot be redistributed; '
        'the synthetic fixture is '
        'reproducible from benchmarks/harness/generate_synthetic_database.py but is not representative of commercial databases.',
        'Uniformly random IPv6 addresses almost never match GeoLite2-City'
        + ('; the populated-network profile weights every network equally regardless of its size, which overrepresents '
           'long prefixes compared with real traffic.' if 'hits' in ctx.timed_profiles
           else ', so random IPv6 results mostly measure misses.'),
        f'Results are specific to this {ctx.arch} CPU, compiler and memory hierarchy; this report has no '
        + (f'{other} or ' if other else '') + '32-bit measurements.',
    ] + ([
        ('Some configurations show parallel efficiency above 100%: APERF cycles per lookup per worker decrease with more '
         'workers. The mechanism (for example memory and fabric power management at low load) was not isolated.')
        if ctx.cycles else
        ('Some configurations show parallel efficiency above 100%. The mechanism was not isolated; no cycle counter '
         'was available to check whether per-lookup cost changes with the number of workers.'),
    ] if ctx.superlinear() else []) + [
        'Cache-miss and branch-miss counters were unavailable; cycles per lookup come from APERF and include any '
        'interference on the pinned CPU.' if ctx.cycles else
        'Cycle, cache-miss and branch-miss counters were unavailable; only wall-clock and thread CPU time were measured.',
    ] + ([
        f'The {clock} advances in steps of about {ctx.timer_step} ns on this CPU '
        f'({grid["on_grid_fraction"] * 100:.1f}% of latency samples fall on '
        f'a {ctx.timer_step} ns grid), so sub-{5 * ctx.timer_step} ns latency percentiles are coarse.',
    ] if ctx.timer_step else [])


def campaign_minutes(ctx):
    """Minutes between the environment snapshots taken before and after the measurements."""
    end = ctx.s.get('environment_end')
    if not end:
        return None
    start, stop = (datetime.datetime.fromisoformat(e['recorded']) for e in (ctx.env, end))
    return max(1, round((stop - start).total_seconds() / 60))


def reproduce(ctx):
    city = ctx.databases['city']
    minutes = campaign_minutes(ctx)
    took = f'; the measurements took {minutes} min on the machine above' if minutes else ''
    ci = ('# GitHub Actions measures x86-64 and AArch64 runners for every pushed benchmarks/* branch\n'
          f'# (.github/workflows/benchmarks.yml); this report comes from run {ctx.ci.get("github_run_id")}.\n'
          if ctx.ci else '')
    return ('# From the repository root. Needs Linux, GCC, Clang, CMake + Ninja, Autotools, OpenSSL headers,\n'
            '# Python 3.11+, Node.js and Chrome. Builds both revisions, runs every campaign and the test suites, then\n'
            f'# regenerates benchmarks/reports{took}.\n'
            'benchmarks/reproduce.sh /path/to/GeoLite2-City.mmdb   '
            f'# build {city["build_date"]}, SHA-256 {city["sha256"][:8]}…{city["sha256"][-6:]}\n' + ci)


# Pull request -------------------------------------------------------------------
# The fork and branch that hold the benchmark tooling on top of After.
REPOSITORY = 'https://github.com/0x00F6/libmaxminddb'
# GitHub Pages of the fork: the workflow publishes every architecture of a benchmarks/<commit> branch there.
PAGES = 'https://0x00f6.github.io/libmaxminddb'


def pr_reproduce(ctx):
    city = ctx.databases['city']
    return '\n'.join([
        f'git clone {REPOSITORY} -b benchmarks/{ctx.after_short}',
        'cd libmaxminddb',
        f'# GeoLite2-City build {city["build_date"]} (SHA-256 {city["sha256"][:8]}…{city["sha256"][-6:]}) cannot be '
        'redistributed: save it as benchmarks/GeoLite2-City.mmdb.',
        'benchmarks/reproduce.sh benchmarks/GeoLite2-City.mmdb'])


def md_table(head, rows, align=None):
    align = align or ['---'] * len(head)
    out = ['| ' + ' | '.join(head) + ' |', '| ' + ' | '.join(align) + ' |']
    out += ['| ' + ' | '.join(str(x) for x in row) + ' |' for row in rows]
    return '\n'.join(out)


def attribution_rows(ctx):
    rows = []
    for a in ctx.s.get('attribution', []):
        r = lambda k: f'{fmt_speedup(a[k]["ratio"])} [{a[k]["ci95_low"]:.2f}–{a[k]["ci95_high"]:.2f}]'
        rows.append([DB_LABEL[a['database']], f'IPv{a["family"]}', PROFILE_TITLE[a['profile']], SIZE_LABEL[a['queries']],
                     a['threads'], fmt_mlps(a['before']['median']), fmt_mlps(a['after-packed']['median']),
                     fmt_mlps(a['after']['median']), r('packed_vs_before'), r('after_vs_packed'), r('after_vs_before')])
    return rows


ATTRIBUTION_HEAD = ['Database', 'Family', 'Profile', 'Queries', 'Threads', 'Before M/s', 'After, NO_PREPARE M/s',
                    'After M/s', 'Packed traversal vs Before', 'Preparation vs packed', 'After vs Before']


def attribution_effect(ctx, key, label):
    rows = [a for a in ctx.s.get('attribution') or [] if a['database'] == 'city']
    if not rows:
        return []
    values = [a[key]['ratio'] for a in rows]
    unclear = [a for a in rows if a[key]['ci95_low'] <= 1 <= a[key]['ci95_high']]
    text = (f'- 🚀 **{label}:** {fmt_speedup(min(values))}–{fmt_speedup(max(values))} on GeoLite2-City lookups '
            f'({len(rows)} configurations: {ctx.scope("attribution", database="city")})')
    if unclear:
        text += (f'; {len(unclear)} of the {len(rows)} intervals include ×1.00 ('
                 + '; '.join(f'IPv{a["family"]} {PROFILE_TITLE[a["profile"]].lower()}, {a["threads"]} '
                             f'thread{"s" if a["threads"] > 1 else ""}' for a in unclear) + ')')
    return [text + '.']


def pr_description(ctx, manifest):
    s = ctx.s
    h = s['headline']['throughput']
    city = h['city']
    title_pct = round(city['geomean_change_percent'])
    by_id = {m['id']: m for m in manifest}
    mem = next(m for m in ctx.s['memory'] if m['database'] == 'city' and m['family'] == 4
               and m['queries'] == SIZE and m['threads'] == 1)
    mem_syn = next(m for m in ctx.s['memory'] if m['database'] == 'synthetic' and m['family'] == 4
                   and m['queries'] == SIZE and m['threads'] == 1)
    start = {(r['database'], r['version'], r['variant'], r['cold']): r for r in s['startup']}
    alloc_before = next(r for r in s['allocations'] if r['database'] == 'city' and r['family'] == 4
                        and r['profile'] == 'hits' and r['operation'] == 'decode' and r['version'] == 'before')
    primary = ctx.one('throughput', database='city', profile='random', operation='lookup', family=4,
                      queries=SIZE, threads=1)
    cycles = lambda v: (primary[v].get('core_cycles_per_lookup') or {}).get('median')
    motivation = {'cycles_before': cycles('before'), 'cycles_after': cycles('after'),
                  'ns_before': 1000 / primary['before']['throughput_mlps']['median'],
                  'ns_after': 1000 / primary['after']['throughput_mlps']['median']}
    commits = [c[:7] for c in s['after']['commits']]
    city_db = ctx.databases['city']
    after_row = f'`{s["after"]["commit"]}` (' + ' + '.join(f'`{c}`' for c in commits) + ').'
    if s['after']['built_from'] != s['after']['commit']:
        after_row += (f' The executables were built from `{s["after"]["built_from"][:7]}`, an earlier commit with the '
                      f'identical source tree `{s["after"]["tree"][:12]}`; building `{commits[-1]}` gives '
                      'byte-identical binaries.')
    modified = [line[3:] for line in s['after'].get('working_tree_status') or [] if not line.startswith('??')]
    after_row += (' The working tree differed only in ' + '/'.join(f'`{m}`' for m in modified) if modified
                  else ' The working tree was clean')

    def fig(figure_id):
        m = by_id[figure_id]
        return f'![{m["alt"]}]({m["svg"]})\n\n*{m["caption"]}*'

    slower = [c for c in s['throughput'] if c['comparison']['direction'] == 'slower']
    inconclusive = [c for c in s['throughput'] if c['comparison']['direction'] == 'inconclusive']
    lat_worse = [c for c in s['latency'] if c['latency_comparison']['p99']['after'] > c['latency_comparison']['p99']['before']]

    def describe_config(c):
        return (f'{DB_LABEL[c["database"]]} IPv{c["family"]} {PROFILE_TITLE[c["profile"]].lower()} '
                f'{OP_TITLE[c["operation"]].lower()}, {SIZE_LABEL[c["queries"]]}, {c["threads"]} thread{"s" if c["threads"] > 1 else ""}')

    out = [f'# Improve performance +{title_pct}%', '',
           '## 🎯 Motivation', '',
           '`MMDB_lookup_sockaddr()` walks the search tree one prefix bit at a time, and every step depends on the '
           'previous one. In the current code each step also decodes a packed 24/28/32-bit record through a function '
           'pointer chosen at runtime, recomputes the node address and branches on the address bit. Below the cached IPv4 '
           'start node an IPv4 lookup still takes up to 32 such steps, an IPv6 lookup up to 128, so per-step overhead and '
           f'dependent loads dominate: a random IPv4 lookup on GeoLite2-City takes {motivation["ns_before"]:.0f} ns on one '
           'thread' + (f', {motivation["cycles_before"]:.0f} core cycles according to APERF' if motivation['cycles_before'] else '')
           + '. Every `MMDB_get_value()` '
           'call additionally allocates and frees a temporary path array: one `calloc` per decoded hit '
           f'({alloc_before["bytes_per_lookup"]:.0f} bytes for `country → iso_code`).', '',
           'Readers are typically opened once and shared by many threads in servers, proxies and log pipelines, so '
           'steady-state lookup throughput and tail latency are what these changes target; the one-time cost moves into '
           '`MMDB_open()` and is quantified below.', '',
           '## ⚡ Optimizations', '',
           f'### 1. 🧭 Specialized packed traversal (`{commits[0]}`)', '',
           '- 🔧 **What:** `find_address_in_search_tree()` dispatches once per lookup on the record width (6/7/8-byte '
           'nodes) and runs a loop with that width as a constant; the child is selected arithmetically '
           '(`record_pointer + 3 * bit` etc.) instead of a branch plus an indirect getter call per bit.',
           '- 💡 **Why it helps:** removes an indirect call and a data-dependent branch from every step of the dependent '
           'chain and lets the compiler inline the 24/28/32-bit decoder.',
           '- 🛡️ **Safety:** bounds are checked as 64-bit byte offsets before a pointer is formed; the mapped tree format, '
           'structure layouts and error codes are unchanged. This is also the path used when preparation is disabled '
           'or cannot be built.',
           '- 🧪 **Validation:** new `t/lookup_specialization_t.c` compares every node encoding and both families with '
           '`MMDB_read_node()`-based traversal, plus truncated-tree checks.',
           ] + attribution_effect(ctx, 'packed_vs_before', 'Effect (separate measurement, After opened with '
                                  '`MMDB_NO_PREPARE`)') + ['',
           f'### 2. 🪶 Allocation-free short `MMDB_get_value()` paths (`{commits[1]}`)', '',
           '- 🔧 **What:** `MMDB_vget_value()` keeps lookup paths of up to seven components in an eight-pointer stack array; '
           'longer paths keep the checked heap allocation.',
           f'- 🚀 **Effect (measured):** {alloc_before["allocations_per_lookup"]:.3f} → 0 allocations per lookup on '
           'hit-only decode workloads (instrumented binaries).',
           '- 🧪 **Validation:** `t/get_value_t.c` checks path lengths 0, 1, 7, 8, 9 and 12, i.e. both sides of the '
           'stack/heap boundary.', '',
           f'### 3. 🗂️ Automatic radix-16 preparation with aligned native nodes (`{commits[2]}`)', '',
           '- 🔧 **What:** `MMDB_open()` now converts the packed tree once into a 64-byte-aligned array of native '
           '`uint32_t[2]` child pairs (8-byte nodes never straddle a cache line) and fills a 65,536-entry table that maps '
           'the first 16 address bits to the reached node and exact depth (plus a second table for the IPv4 subtree of '
           'IPv6 databases). `MMDB_lookup_sockaddr()`/`MMDB_lookup_string()` then skip up to 16 levels with one load and '
           'walk the native array without decoding packed records.',
           '- 🔢 **Format readers:** preparation uses direct 24/28/32-bit decoders over the sequential tree.',
           '- 🧱 **Ownership/ABI:** the prepared state lives in a private allocation reached through the reader\'s filename '
           'allocation and a private flag bit; `MMDB_s` keeps its layout and the SONAME stays `0` (additive '
           '`-version-info 1:0:1`). An old executable built against the original header runs unchanged '
           '(`t/abi_compat_t.c`, `t/abi_legacy_client.c`).',
           '- 🪂 **Fallbacks:** if preparation cannot allocate or the native array would exceed 64 MiB, lookups use the '
           'specialized packed traversal. `MMDB_NO_PREPARE` opts out. Explicit `MMDB_create_lookup_context()` / '
           '`MMDB_lookup_*_with_context()` APIs expose the same structure for callers that manage it themselves.',
           '- ✅ **Correctness:** early terminals fill the whole remaining key range with their exact depth, so prefix '
           'lengths and no-match results are unchanged; corrupt branches are reported only when a lookup reaches them.',
           ] + attribution_effect(ctx, 'after_vs_packed', 'Effect (separate measurement, on top of optimization 1)') + [
           '- 🧪 **Validation:** `t/lookup_context_t.c` compares prepared, explicit-context and packed lookups (random '
           'sockaddr and string queries, exact prefixes, contexts shared by threads, corruption and lifetime cases); '
           '`t/abi_compat_t.c` checks the frozen public layouts and `t/abi_legacy_client.c` runs a client built with the '
           'original header.', '',
           fig('radix16-preparation'), '',
           '## 📊 Benchmarks', '',
           f'🌐 **Complete benchmarks on x86-64 and AArch64:** {PAGES}/{ctx.after_short}/performance-comparison-benchmarks-{ctx.after_short}.html '
           '(GitHub-hosted runners; switch with the Architecture filter). The environment and charts below come from the '
           f'{ctx.cpu} campaign.', '',
           md_table(['', ''], [
               ['Before', f'`{s["before"]["commit"]}` (`main`, Merge pull request #512)'],
               ['After', after_row],
               ['CPU', f'**{ctx.cpu}** — {ctx.cores} cores / {ctx.logical} threads, ' + ', '.join(
                   ['SMT on' if ctx.smt else 'no SMT']
                   + ([f'boost {"on" if ctx.env["cpu"]["boost"] == "1" else "off"}'] if ctx.env['cpu'].get('boost') is not None else [])
                   + ([f'governor {ctx.governor}'] if ctx.governor else ['no cpufreq interface']))],
               ['System', f'{ctx.env["memory"]["MemTotal_kib"] / 1024 / 1024:.1f} GiB RAM, {ctx.os}, Linux {ctx.kernel}, '
                          + (f'{ctx.virtualization} VM' + (' (GitHub-hosted runner)' if ctx.ci else '')
                             if ctx.virtualization not in ('none', 'unknown') else 'no VM') + ', '
                          + ('no cgroup CPU quota' if ctx.env['os']['cgroup']['cpu.max'].startswith(('max', 'not present'))
                             else f'cgroup CPU quota {ctx.env["os"]["cgroup"]["cpu.max"]}')],
               ['Build', f'GCC {ctx.gcc}, CMake Release `-O3 -DNDEBUG`, static libraries, same harness source'],
               ['Databases', f'GeoLite2-City build {city_db["build_date"]} ({city_db["node_count"]:,} nodes, '
                             f'{city_db["record_size"]}-bit records); deterministic synthetic fixture from '
                             f'`generate_synthetic_database.py` ({ctx.databases["synthetic"]["node_count"]:,} nodes)'],
               ['Workloads', f'{SIZE_LABEL[SIZE]} queries in total (split across '
                             f'{joined(present(s["throughput"], "threads", THREADS))} pinned workers); '
                             f'{joined(PROFILE_NAME[p] for p in ctx.timed_profiles)} addresses; '
                             f'{joined(OP_CALL[o] for o in present(s["throughput"], "operation", OPERATIONS))}'],
           ]), '',
           f'**Protocol.** {ctx.reps} measured repetitions per configuration with alternating Before/After order (one prepared '
           'process per version, triggered one repetition at a time), warm-up before every repetition, a spin start gate '
           'and wall-clock timing from release to the last worker. Throughput runs carry no per-query timers; latency is a '
           f'separate campaign that times every {s["protocol"]["latency_sampling_stride"]}th lookup and merges exact histograms '
           'across workers and repetitions. Medians with 95% bootstrap intervals; speedup = After / Before median. Memory and '
           'startup use fresh processes.', '',
           '### Throughput — GeoLite2-City, random lookups', '',
           fig(fid('throughput', 4, SIZE)), '', fig(fid('throughput', 6, SIZE)), '',
           f'### Latency — GeoLite2-City, random lookups, {SIZE_LABEL[SIZE]} queries', '',
           fig(fid('candlestick', 4, SIZE)), '', fig(fid('candlestick', 6, SIZE)), '',
           '### Memory, startup and allocations', '',
           fig('memory-usage'), '',
           fig('startup-time'), '',
           fig('allocations'), '',
           '## ⚖️ Trade-offs and limitations', '',
           f'- ⏱️ **Slower startup (`{commits[2]}`):** `MMDB_open()` on GeoLite2-City takes '
           f'{fmt_ms(start[("city", "after", "default", False)]["open_ms"]["median"])} instead of '
           f'{fmt_ms(start[("city", "before", "default", False)]["open_ms"]["median"])} with a warm page cache '
           f'({fmt_ms(start[("city", "after", "default", True)]["open_ms"]["median"])} instead of '
           f'{fmt_ms(start[("city", "before", "default", True)]["open_ms"]["median"])} cold). Short-lived programs such as '
           '`mmdblookup` pay it for a few lookups; `MMDB_NO_PREPARE` restores the old cost.',
           f'- 💾 **More memory (`{commits[2]}`):** +{mem["comparison"]["heap_after_open_mib"]["delta"]:.1f} MiB of private '
           f'heap per open GeoLite2-City reader (+{mem_syn["comparison"]["heap_after_open_mib"]["delta"]:.1f} MiB on the '
           'synthetic fixture), paid again by every process that opens the database.',
           ]
    if lat_worse and all(c['database'] == 'city' and c['family'] == 6 and c['profile'] == 'random' for c in lat_worse):
        example = next((c for c in lat_worse if c['operation'] == 'lookup' and c['threads'] == 1),
                       lat_worse[0])
        p99 = example['latency_comparison']['p99']
        out.append(f'- 📈 **Longer p99 on random IPv6 lookups (`{commits[2]}`):** {len(lat_worse)} of {len(s["latency"])} '
                   'latency configurations, all uniformly random IPv6 lookups on GeoLite2-City that almost never match '
                   f'({fmt_hits(max(c["hit_ratio"] for c in lat_worse))} hits), for example {fmt_ns(p99["before"])} → '
                   f'{fmt_ns(p99["after"])} on {example["threads"]} '
                   f'thread{"s" if example["threads"] > 1 else ""}. Their p50 and p95 still drop; misses on the 512 KiB radix '
                   'table are a plausible but unconfirmed cause.')
    elif lat_worse:
        out.append(f'- 📈 **Higher p99 ({len(lat_worse)} of {len(s["latency"])} latency configurations):** ' + '; '.join(
            f'{describe_config(c)} ({fmt_ns(c["latency_comparison"]["p99"]["before"])} → '
            f'{fmt_ns(c["latency_comparison"]["p99"]["after"])})' for c in lat_worse) + '.')
    if slower:
        out.append(f'- ⚠️ **Throughput regressions ({len(slower)}):** ' + '; '.join(
            f'{describe_config(c)} ({fmt_speedup(c["comparison"]["speedup"])})' for c in slower) + '.')
    if inconclusive:
        out.append(f'- ❔ **Inconclusive throughput differences ({len(inconclusive)}):** ' + '; '.join(
            f'{describe_config(c)} ({fmt_speedup(c["comparison"]["speedup"])})' for c in inconclusive) + '.')
    attribution = [a for a in s.get('attribution') or [] if a['database'] == 'city']
    if attribution:
        packed = [a['packed_vs_before']['ratio'] for a in attribution]
        full = [a['after_vs_before']['ratio'] for a in attribution]
        out.append(f'- 🐢 **Smaller gain without preparation (`{commits[0]}`):** with `MMDB_NO_PREPARE`, or when preparation '
                   'cannot allocate or its native copy would exceed 64 MiB, lookups use only the specialized packed '
                   f'traversal: {fmt_speedup(min(packed))}–{fmt_speedup(max(packed))} on GeoLite2-City instead of '
                   f'{fmt_speedup(min(full))}–{fmt_speedup(max(full))} with preparation ({len(attribution)} configurations, '
                   'separate measurement).')
    out.append(f'- 📦 **Short paths only (`{commits[1]}`):** `MMDB_get_value()` avoids its allocation for paths of up to '
               'seven components; longer paths still allocate.')
    minutes = campaign_minutes(ctx)
    out += ['', '## 🔁 Reproduction', '', '```sh', pr_reproduce(ctx), '```', '',
            'Needs Linux, GCC, Clang, CMake + Ninja, Autotools, OpenSSL headers, Python 3.11+, Node.js and Chrome. '
            'The script builds both revisions, runs every campaign and the test suites, then regenerates '
            '`benchmarks/reports`' + (f'; the measurements took {minutes} min on the machine above.' if minutes else '.'), '',
            'Full report: `benchmarks/reports/performance-comparison.html` (interactive); raw per-run JSON/CSV in '
            '`benchmarks/reports/performance-comparison-data/`. `benchmarks/reporting/generate.sh` regenerates the '
            'reports from the saved results without measuring.', '']
    return '\n'.join(out)


def write_manifest(manifest):
    for m in manifest:
        for key in ('png', 'svg'):
            path = REPORTS / m[key]
            m[f'{key}_sha256'] = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
    (ASSETS / 'manifest.json').write_text(json.dumps(manifest, indent=1, ensure_ascii=False) + '\n')
    lines = ['# Image manifest — performance comparison', '',
             'Each figure exists as an SVG and as a PNG rendered at twice its size. `../pull-request-description.md` '
             'embeds some of the SVGs through relative paths. No image is hosted yet: upload each SVG it references (or '
             'its PNG) in the merge/pull request editor and replace the relative path in '
             'the draft with the uploaded URL.', '',
             md_table(['Figure', 'PNG', 'SVG', 'Pixels', 'Hosted URL'],
                      [[m['title'], f'`{Path(m["png"]).name}`', f'`{Path(m["svg"]).name}`',
                        f'{m["png_size"][0]}×{m["png_size"][1]}', 'not hosted'] for m in manifest]),
             '', '## Captions and alternative text', '']
    for m in manifest:
        lines += [f'### {m["title"]}', '', f'- PNG: `{Path(m["png"]).name}` (SHA-256 `{m.get("png_sha256")}`)',
                  f'- SVG: `{Path(m["svg"]).name}` (SHA-256 `{m.get("svg_sha256")}`)', f'- Alt text: {m["alt"]}',
                  f'- Caption: {m["caption"]}', '']
    (ASSETS / 'MANIFEST.md').write_text('\n'.join(lines))


def configure(summary):
    """Read the campaign's workload size and worker counts."""
    global SIZE, THREADS
    SIZE = workload_size(summary)
    THREADS = tuple(sorted({c['threads'] for c in summary['throughput']}))


def workload_size(summary):
    sizes = {c['queries'] for c in summary['throughput']}
    if len(sizes) != 1:
        raise SystemExit(f'expected one workload size in the throughput results, found {sorted(sizes)}')
    return sizes.pop()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--summary', default=str(DATA / 'summary.json'))
    parser.add_argument('--output', default=str(REPORTS), help='directory receiving the reports and assets')
    parser.add_argument('--merge', metavar='SITE',
                        help='write one report of every architecture directory in SITE to --output (an HTML file)')
    args = parser.parse_args()
    if args.merge:
        Path(args.output).write_text(merged_report(args.merge))
        print(f'Wrote {args.output}')
        return
    set_output(args.output)
    summary = json.loads(Path(args.summary).read_text())
    configure(summary)
    ctx = Context(summary)
    manifest = write_exports(ctx)
    HTML_OUT.write_text(html_report(ctx, manifest))
    PR_OUT.write_text(pr_description(ctx, manifest))
    write_manifest(manifest)
    print(f'Wrote {HTML_OUT}, {PR_OUT} and {len(manifest)} SVG exports')


if __name__ == '__main__':
    main()
