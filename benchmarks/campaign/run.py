#!/usr/bin/env python3
"""Run the Before/After benchmark campaigns (Linux only).

Timed campaigns run one configuration at a time. Paired campaigns start one
prepared process per version and trigger exactly one measured repetition at a
time; even pairs run Before then After, odd pairs reverse the order. Startup
uses fresh processes, one at a time. Memory, allocation and differential
campaigns time nothing, so their fresh processes run --jobs at a time, each
pinned to its own CPU. Results are appended to JSON Lines files and existing
complete configurations are never overwritten; --resume skips them and reruns
incomplete ones.
"""
import argparse
import datetime
import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_WORK = ROOT / 'benchmarks/artifacts/campaign'
VERSIONS = ('before', 'after')
DATABASES = {
    'city': {'file': 'GeoLite2-City.mmdb', 'path': 'country/iso_code',
             'label': 'GeoLite2-City'},
    'synthetic': {'file': 'synthetic.mmdb', 'path': 'id',
                  'label': 'Synthetic mixed'},
}
FAMILIES = (4, 6)
THREADS = (1, 4, 8)
SIZES = (10_000_000,)
PROFILES = ('random', 'hits')
OPERATIONS = ('lookup', 'decode')
SEED = 0x6A09E667F3BCC909
WARMUP = 1_000_000
STRIDE = 16


# Progress lines show the time since reproduce.sh started, or since this script started.
START = float(os.environ.get('REPRODUCE_START') or time.time())


def utc_now():
    return datetime.datetime.now(datetime.UTC).isoformat(timespec='milliseconds')


def elapsed():
    seconds = int(time.time() - START)
    return f'{seconds // 60}:{seconds % 60:02d}'


def default_jobs(cpus):
    """One untimed process per CPU, as long as each can have 768 MiB of available memory."""
    for line in Path('/proc/meminfo').read_text().splitlines():
        if line.startswith('MemAvailable:'):
            return max(1, min(len(cpus), int(line.split()[1]) // (768 * 1024)))
    return 1


def preferred_cpus():
    """First logical CPU of every allowed physical core, best CPPC rank first."""
    allowed = sorted(os.sched_getaffinity(0))
    cores = {}
    for cpu in allowed:
        base = Path(f'/sys/devices/system/cpu/cpu{cpu}')
        try:
            core = (int((base / 'topology/physical_package_id').read_text()),
                    int((base / 'topology/core_id').read_text()))
        except OSError:
            core = (0, cpu)
        try:
            rank = int((base / 'acpi_cppc/highest_perf').read_text())
        except (OSError, ValueError):
            rank = 0
        if core not in cores:
            cores[core] = (rank, cpu)
    return [cpu for rank, cpu in sorted(cores.values(), key=lambda x: (-x[0], x[1]))]


class HostMonitor(threading.Thread):
    """Samples system load without touching benchmark processes."""

    def __init__(self, path, interval=5.0, top_every=12):
        super().__init__(daemon=True)
        self.path, self.interval, self.top_every = path, interval, top_every
        self.stop_event = threading.Event()

    @staticmethod
    def cpu_times():
        fields = Path('/proc/stat').read_text().split('\n', 1)[0].split()[1:]
        values = [int(v) for v in fields]
        idle = values[3] + values[4]
        return sum(values), idle

    @staticmethod
    def process_times():
        times = {}
        for entry in os.scandir('/proc'):
            if not entry.name.isdigit():
                continue
            try:
                stat = Path(entry.path, 'stat').read_text()
            except OSError:
                continue
            name = stat[stat.find('(') + 1:stat.rfind(')')]
            rest = stat[stat.rfind(')') + 2:].split()
            times[int(entry.name)] = (name, int(rest[11]) + int(rest[12]))
        return times

    def run(self):
        last_total, last_idle = self.cpu_times()
        last_processes = self.process_times()
        last_top = time.monotonic()
        count = 0
        with open(self.path, 'a') as output:
            while not self.stop_event.wait(self.interval):
                total, idle = self.cpu_times()
                busy = 1 - (idle - last_idle) / max(1, total - last_total)
                last_total, last_idle = total, idle
                record = {'time': utc_now(),
                          'loadavg': Path('/proc/loadavg').read_text().split()[:3],
                          'system_cpu_busy': round(busy, 4)}
                for line in Path('/proc/meminfo').read_text().splitlines():
                    if line.startswith(('MemAvailable:', 'SwapFree:')):
                        key, value = line.split(':')
                        record[key] = int(value.split()[0])
                count += 1
                if count % self.top_every == 0:
                    now = time.monotonic()
                    processes = self.process_times()
                    ticks = os.sysconf('SC_CLK_TCK') * (now - last_top)
                    usage = []
                    for pid, (name, cpu) in processes.items():
                        if pid in last_processes and last_processes[pid][0] == name:
                            usage.append((cpu - last_processes[pid][1], name, pid))
                    usage.sort(reverse=True)
                    record['top_processes'] = [
                        {'pid': pid, 'name': name, 'cpu_percent': round(100 * d / ticks, 1)}
                        for d, name, pid in usage[:8] if d > 0]
                    last_processes, last_top = processes, now
                output.write(json.dumps(record) + '\n')
                output.flush()

    def stop(self):
        self.stop_event.set()
        self.join()


def config_key(row):
    return tuple(row.get(k) for k in (
        'database', 'campaign', 'profile', 'operation', 'family', 'queries', 'threads'))


class Runner:
    def __init__(self, args):
        self.args = args
        self.work = Path(args.work).resolve()
        self.raw = Path(args.output).resolve()
        self.raw.mkdir(parents=True, exist_ok=True)
        self.cpus = args.cpus or preferred_cpus()
        self.jobs = args.jobs or default_jobs(self.cpus)
        self.log = open(self.raw / 'runner.log', 'a')

    def binary(self, version, alloc=False):
        return str(Path(self.args.bin or self.work / 'bin') / f'bench-{version}{"-alloc" if alloc else ""}')

    def database(self, name):
        return str(self.work / 'data' / DATABASES[name]['file'])

    def note(self, message):
        print(f'[{elapsed()}]   {message}', flush=True)
        self.log.write(f'{utc_now()} {message}\n')
        self.log.flush()

    def base_command(self, version, db, family, queries, threads, profile,
                     operation, campaign, reps, alloc=False, cpus=None):
        return [self.binary(version, alloc), '--db', self.database(db),
                '--family', str(family), '--threads', str(threads),
                '--queries', str(queries), '--profile', profile,
                '--operation', operation, '--path', DATABASES[db]['path'],
                '--campaign', campaign, '--reps', str(reps),
                '--seed', str(SEED), '--warmup', str(self.args.warmup),
                '--stride', str(self.args.stride),
                '--cpus', ','.join(str(c) for c in (cpus or self.cpus))]

    @staticmethod
    def check_header(header, version):
        prepared = header['private_prepared_flag']
        if version == 'before' and prepared:
            raise RuntimeError('Before reader unexpectedly reports prepared state')
        if version == 'after' and not prepared:
            raise RuntimeError('After reader did not prepare its lookup state')

    # Paired throughput and latency campaigns ---------------------------------
    def paired(self, campaign):
        path = self.raw / campaign / 'runs.jsonl'
        path.parent.mkdir(exist_ok=True)
        done = self.load_complete(path, 2 * self.args.reps)
        headers = open(path.parent / 'headers.jsonl', 'a')
        rows = open(path, 'a')
        configs = [(db, profile, operation, family, queries, threads)
                   for db in self.args.databases
                   for profile in self.args.profiles
                   for operation in self.args.operations
                   for family in self.args.families
                   for queries in self.args.sizes
                   for threads in self.args.threads]
        campaign_start = time.monotonic()
        for index, (db, profile, operation, family, queries, threads) in enumerate(configs):
            key = (db, campaign, profile, operation, family, queries, threads)
            if key in done:
                continue
            self.note(f'{campaign} {index + 1}/{len(configs)} {db} {profile} '
                      f'{operation} IPv{family} {queries} x{threads}')
            processes, header = {}, {}
            stderr = open(path.parent / 'stderr.log', 'a')
            for version in VERSIONS:
                command = self.base_command(version, db, family, queries, threads,
                                            profile, operation, campaign,
                                            self.args.reps) + ['--interactive']
                processes[version] = subprocess.Popen(
                    command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=stderr, text=True, bufsize=1)
                processes[version].command = command
            for version in VERSIONS:
                line = processes[version].stdout.readline()
                if not line:
                    raise RuntimeError(f'{version} exited before its header')
                header[version] = json.loads(line)
                self.check_header(header[version], version)
                headers.write(json.dumps({'database': db, 'campaign': campaign,
                                          'version': version, 'time': utc_now(),
                                          'argv': processes[version].command,
                                          **header[version]}) + '\n')
            headers.flush()
            if header['before']['corpus_sha256'] != header['after']['corpus_sha256']:
                raise RuntimeError('Before and After corpora differ')
            for pair in range(self.args.reps):
                order = VERSIONS if pair % 2 == 0 else tuple(reversed(VERSIONS))
                for position, version in enumerate(order):
                    started = utc_now()
                    processes[version].stdin.write('\n')
                    processes[version].stdin.flush()
                    line = processes[version].stdout.readline()
                    if not line:
                        raise RuntimeError(f'{version} exited during pair {pair}')
                    result = json.loads(line)
                    row = {'database': db, 'campaign': campaign, 'profile': profile,
                           'operation': operation, 'family': family,
                           'queries': queries, 'threads': threads,
                           'version': version, 'pair': pair, 'position': position,
                           'started': started, 'finished': utc_now(),
                           'corpus_sha256': header[version]['corpus_sha256'],
                           'decode_path': header[version]['decode_path'],
                           'warmup': header[version]['warmup'],
                           'stride': header[version]['stride'],
                           'oversubscribed': header[version]['oversubscribed'],
                           **{k: v for k, v in result.items() if k != 'type'}}
                    rows.write(json.dumps(row) + '\n')
                    rows.flush()
            for version in VERSIONS:
                processes[version].stdin.close()
                if processes[version].wait() != 0:
                    raise RuntimeError(f'{version} exited with an error')
            stderr.close()
        rows.close()
        headers.close()
        self.note(f'{campaign} done: {len(configs)} configuration{"s" if len(configs) != 1 else ""} in {time.monotonic() - campaign_start:.0f} s')

    def load_complete(self, path, expected):
        """Keep complete configurations; move incomplete ones aside."""
        if not path.exists():
            return set()
        if not self.args.resume:
            raise SystemExit(f'{path} exists; use --resume or another --output')
        rows = [json.loads(line) for line in path.read_text().splitlines() if line]
        counts = {}
        for row in rows:
            counts[config_key(row)] = counts.get(config_key(row), 0) + 1
        complete = {key for key, count in counts.items() if count == expected}
        partial = [row for row in rows if config_key(row) not in complete]
        if partial:
            with open(path.parent / 'discarded-incomplete.jsonl', 'a') as output:
                for row in partial:
                    output.write(json.dumps(row) + '\n')
            with open(path, 'w') as output:
                for row in rows:
                    if config_key(row) in complete:
                        output.write(json.dumps(row) + '\n')
            self.note(f'Discarded {len(partial)} rows of incomplete configurations')
        return complete

    # Fresh-process campaigns -------------------------------------------------
    def fresh(self, command):
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f'{command} failed: {result.stderr}')
        return [json.loads(line) for line in result.stdout.splitlines() if line]

    def fresh_parallel(self, campaign, builders):
        """Run untimed fresh processes self.jobs at a time; job slot k pins its
        first worker to self.cpus[k]. Returns (argv, output lines) in order."""
        slots = list(range(self.jobs))
        lock = threading.Lock()

        def run(build):
            with lock:
                slot = slots.pop()
            try:
                command = build(self.cpus[slot:] + self.cpus[:slot])
                return command, self.fresh(command)
            finally:
                with lock:
                    slots.append(slot)

        campaign_start = time.monotonic()
        self.note(f'{campaign}: {len(builders)} processes, {self.jobs} at a time')
        with ThreadPoolExecutor(self.jobs) as pool:
            results = list(pool.map(run, builders))
        self.note(f'{campaign} done in {time.monotonic() - campaign_start:.0f} s')
        return results

    def memory(self):
        path = self.raw / 'memory' / 'runs.jsonl'
        path.parent.mkdir(exist_ok=True)
        if path.exists() and not self.args.resume:
            raise SystemExit(f'{path} exists')
        done = set()
        if path.exists():
            for line in path.read_text().splitlines():
                row = json.loads(line)
                done.add((row['database'], row['family'], row['queries'],
                          row['threads'], row['iteration'], row['version']))
        plan = [(db, family, queries, threads, iteration, version)
                for db in self.args.databases
                for family in self.args.families
                for queries in self.args.sizes
                for threads in self.args.threads
                for iteration in range(self.args.memory_iterations)
                for version in (VERSIONS if iteration % 2 == 0 else tuple(reversed(VERSIONS)))
                if (db, family, queries, threads, iteration, version) not in done]

        def builder(db, family, queries, threads, iteration, version):
            return lambda cpus: self.base_command(version, db, family, queries, threads, 'random',
                                                  'lookup', 'throughput', 1, cpus=cpus)
        results = self.fresh_parallel('memory', [builder(*item) for item in plan])
        with open(path, 'a') as output:
            for (db, family, queries, threads, iteration, version), (command, (header, result)) in zip(plan, results):
                self.check_header(header, version)
                output.write(json.dumps({
                    'database': db, 'family': family, 'queries': queries, 'threads': threads,
                    'profile': 'random', 'operation': 'lookup', 'iteration': iteration,
                    'version': version, 'time': utc_now(), 'argv': command,
                    'header': header, 'result': result}) + '\n')

    def startup(self):
        path = self.raw / 'startup' / 'runs.jsonl'
        path.parent.mkdir(exist_ok=True)
        if path.exists() and not self.args.resume:
            raise SystemExit(f'{path} exists')
        variants = [('before', 'default'), ('after', 'default'),
                    ('after', 'no-prepare'), ('after', 'context')]
        plan = [(cold, iteration)
                for cold, iterations in ((False, self.args.startup_warm),
                                         (True, self.args.startup_cold))
                for iteration in range(iterations)]
        with open(path, 'a') as output:
            for db in self.args.databases:
                for cold, iteration in plan:
                    rotation = iteration % len(variants)
                    for version, variant in variants[rotation:] + variants[:rotation]:
                        command = ['taskset', '-c', str(self.cpus[0]),
                                   self.binary(version), '--mode', 'open',
                                   '--db', self.database(db),
                                   '--open-variant', variant]
                        if cold:
                            command.append('--cold')
                        (result,) = self.fresh(command)
                        output.write(json.dumps({
                            **{k: v for k, v in result.items() if k != 'type'},
                            'database_path': result['database'],
                            'database': db, 'version': version, 'variant': variant,
                            'cold': cold, 'iteration': iteration, 'time': utc_now(),
                            'argv': command}) + '\n')
                        output.flush()
                self.note(f'startup {db} done ({len(plan) * len(variants)} opens)')

    def allocations(self):
        path = self.raw / 'allocations' / 'runs.jsonl'
        path.parent.mkdir(exist_ok=True)
        if path.exists() and not self.args.resume:
            raise SystemExit(f'{path} exists')
        plan = [(db, family, profile, operation, queries, version)
                for db in self.args.databases
                for family in self.args.families
                for profile in self.args.profiles
                for operation in self.args.operations
                for queries in self.args.sizes
                for version in VERSIONS]

        def builder(db, family, profile, operation, queries, version):
            return lambda cpus: self.base_command(version, db, family, queries, 1, profile, operation,
                                                  'throughput', self.args.allocation_reps,
                                                  alloc=True, cpus=cpus)
        results = self.fresh_parallel('allocations', [builder(*item) for item in plan])
        with open(path, 'a') as output:
            for (db, family, profile, operation, queries, version), (command, (header, *runs)) in zip(plan, results):
                for result in runs:
                    output.write(json.dumps({
                        'database': db, 'family': family, 'profile': profile,
                        'operation': operation, 'queries': queries, 'version': version,
                        'time': utc_now(), 'argv': command, 'header': header,
                        'result': result}) + '\n')

    def attribution(self):
        """Three-way rotation: Before, After opened with MMDB_NO_PREPARE (the
        specialized packed traversal alone) and After (automatic preparation)."""
        path = self.raw / 'attribution' / 'runs.jsonl'
        path.parent.mkdir(exist_ok=True)
        variants = (('before', 'before', []), ('after-packed', 'after', ['--no-prepare']),
                    ('after', 'after', []))
        done = self.load_complete(path, len(variants) * self.args.reps)
        headers = open(path.parent / 'headers.jsonl', 'a')
        rows = open(path, 'a')
        configs = [(db, profile, family, queries, threads)
                   for db in self.args.databases for profile in self.args.profiles
                   for family in self.args.families for queries in self.args.sizes
                   for threads in self.args.threads]
        campaign_start = time.monotonic()
        for index, (db, profile, family, queries, threads) in enumerate(configs):
            key = (db, 'attribution', profile, 'lookup', family, queries, threads)
            if key in done:
                continue
            self.note(f'attribution {index + 1}/{len(configs)} {db} {profile} IPv{family} {queries} x{threads}')
            processes, header = {}, {}
            stderr = open(path.parent / 'stderr.log', 'a')
            for name, binary, extra in variants:
                command = self.base_command(binary, db, family, queries, threads, profile, 'lookup',
                                            'throughput', self.args.reps) + extra + ['--interactive']
                processes[name] = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                                   stderr=stderr, text=True, bufsize=1)
                processes[name].command = command
            for name, binary, extra in variants:
                line = processes[name].stdout.readline()
                if not line:
                    raise RuntimeError(f'{name} exited before its header')
                header[name] = json.loads(line)
                if header[name]['private_prepared_flag'] != (name == 'after'):
                    raise RuntimeError(f'{name} has an unexpected preparation state')
                headers.write(json.dumps({**header[name], 'database': db, 'campaign': 'attribution',
                                          'version': name, 'time': utc_now(),
                                          'argv': processes[name].command}) + '\n')
            headers.flush()
            if len({h['corpus_sha256'] for h in header.values()}) != 1:
                raise RuntimeError('Attribution corpora differ')
            for pair in range(self.args.reps):
                rotation = pair % len(variants)
                order = variants[rotation:] + variants[:rotation]
                for position, (name, binary, extra) in enumerate(order):
                    started = utc_now()
                    processes[name].stdin.write('\n')
                    processes[name].stdin.flush()
                    line = processes[name].stdout.readline()
                    if not line:
                        raise RuntimeError(f'{name} exited during pair {pair}')
                    result = json.loads(line)
                    rows.write(json.dumps({
                        **{k: v for k, v in result.items() if k != 'type'},
                        'database': db, 'campaign': 'attribution', 'profile': profile,
                        'operation': 'lookup', 'family': family, 'queries': queries,
                        'threads': threads, 'version': name, 'pair': pair, 'position': position,
                        'started': started, 'finished': utc_now(),
                        'corpus_sha256': header[name]['corpus_sha256'],
                        'warmup': header[name]['warmup']}) + '\n')
                    rows.flush()
            for name in processes:
                processes[name].stdin.close()
                if processes[name].wait() != 0:
                    raise RuntimeError(f'{name} exited with an error')
            stderr.close()
        rows.close()
        headers.close()
        self.note(f'attribution done: {len(configs)} configuration{"s" if len(configs) != 1 else ""} in {time.monotonic() - campaign_start:.0f} s')

    def verify(self):
        path = self.raw / 'verify' / 'runs.jsonl'
        path.parent.mkdir(exist_ok=True)
        if path.exists() and not self.args.resume:
            raise SystemExit(f'{path} exists')
        plan = [(db, family, profile, queries, version)
                for db in self.args.databases
                for family in self.args.families
                for profile in self.args.profiles
                for queries in self.args.sizes
                for version in VERSIONS]

        def builder(db, family, profile, queries, version):
            return lambda cpus: ['taskset', '-c', str(cpus[0])] + self.base_command(
                version, db, family, queries, 1, profile, 'decode', 'throughput', 1,
                cpus=cpus[:1]) + ['--mode', 'verify']
        results = self.fresh_parallel('verify', [builder(*item) for item in plan])
        digests = {}
        with open(path, 'a') as output:
            for (db, family, profile, queries, version), (command, (header,)) in zip(plan, results):
                digests.setdefault((db, family, profile, queries), {})[version] = header['result_sha256']
                output.write(json.dumps({
                    **header, 'database_path': header['database'],
                    'database': db, 'family': family, 'profile': profile,
                    'queries': queries, 'version': version,
                    'time': utc_now(), 'argv': command}) + '\n')
        for (db, family, profile, queries), digest in digests.items():
            if digest['before'] != digest['after']:
                raise RuntimeError(f'Differential mismatch: {db} {family} {profile} {queries}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('campaigns', nargs='+',
                        choices=['verify', 'startup', 'allocations', 'throughput',
                                 'latency', 'memory', 'attribution'])
    parser.add_argument('--work', default=str(DEFAULT_WORK))
    parser.add_argument('--bin', help='directory of bench-* executables (default: WORK/bin)')
    parser.add_argument('--output', default=str(DEFAULT_WORK / 'raw'))
    parser.add_argument('--databases', nargs='+', default=list(DATABASES), choices=list(DATABASES))
    parser.add_argument('--families', nargs='+', type=int, default=list(FAMILIES))
    parser.add_argument('--threads', nargs='+', type=int, default=list(THREADS))
    parser.add_argument('--sizes', nargs='+', type=int, default=list(SIZES))
    parser.add_argument('--profiles', nargs='+', default=list(PROFILES), choices=list(PROFILES))
    parser.add_argument('--operations', nargs='+', default=list(OPERATIONS), choices=list(OPERATIONS))
    parser.add_argument('--reps', type=int, default=10)
    parser.add_argument('--warmup', type=int, default=WARMUP)
    parser.add_argument('--stride', type=int, default=STRIDE)
    parser.add_argument('--cpus', nargs='+', type=int)
    parser.add_argument('--memory-iterations', type=int, default=5)
    parser.add_argument('--allocation-reps', type=int, default=1,
                        help='measured repetitions per allocation-counting process (counts are deterministic)')
    parser.add_argument('--jobs', type=int,
                        help='untimed processes run at once (default: one per CPU, given 768 MiB each)')
    parser.add_argument('--startup-warm', type=int, default=30)
    parser.add_argument('--startup-cold', type=int, default=10)
    parser.add_argument('--resume', action='store_true')
    args = parser.parse_args()
    runner = Runner(args)
    monitor = HostMonitor(runner.raw / 'host-activity.jsonl')
    monitor.start()
    runner.log.write(f'{utc_now()} start campaigns={args.campaigns} cpus={runner.cpus} argv={sys.argv}\n')
    try:
        for campaign in args.campaigns:
            if campaign in ('throughput', 'latency'):
                runner.paired(campaign)
            else:
                getattr(runner, campaign)()
    finally:
        monitor.stop()
        runner.log.write(f'{utc_now()} stop\n')
        runner.log.flush()


if __name__ == '__main__':
    main()
