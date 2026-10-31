#!/usr/bin/env python3
"""Record the measurement machine, toolchain, binaries and databases as JSON."""
import datetime
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORK = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / 'benchmarks/artifacts/campaign'
# Relevant x86-64 flags and AArch64 features, as /proc/cpuinfo names them.
FLAGS = ('sse4_2', 'avx', 'avx2', 'bmi1', 'bmi2', 'avx512f', 'avx512bw',
         'avx512vl', 'popcnt', 'lzcnt', 'abm', 'movbe', 'rdpru', 'constant_tsc',
         'nonstop_tsc',
         'asimd', 'atomics', 'crc32', 'lrcpc', 'sve', 'sve2')


def read(path, default=None):
    try:
        return Path(path).read_text().strip()
    except OSError:
        return default


def command(*argv):
    try:
        return subprocess.run(argv, capture_output=True, text=True, check=False).stdout.strip()
    except OSError:
        return None


def sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for block in iter(lambda: stream.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def integer(value, default):
    """lscpu prints "-" for some AArch64 fields, such as Socket(s)."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def lscpu():
    fields = {}
    for line in command('lscpu').splitlines():
        key, _, value = line.partition(':')
        fields[key.strip()] = value.strip()
    return fields


def cpu_topology():
    cpus = []
    for cpu in sorted(os.sched_getaffinity(0)):
        base = f'/sys/devices/system/cpu/cpu{cpu}'
        cpus.append({
            'cpu': cpu,
            'core_id': int(read(f'{base}/topology/core_id', '-1')),
            'package_id': int(read(f'{base}/topology/physical_package_id', '-1')),
            'thread_siblings': read(f'{base}/topology/thread_siblings_list'),
            'cppc_highest_perf': read(f'{base}/acpi_cppc/highest_perf'),
            'governor': read(f'{base}/cpufreq/scaling_governor'),
            'energy_performance_preference': read(f'{base}/cpufreq/energy_performance_preference'),
            'max_freq_khz': read(f'{base}/cpufreq/cpuinfo_max_freq'),
        })
    return cpus


def meminfo():
    values = {}
    for line in read('/proc/meminfo', '').splitlines():
        key, _, value = line.partition(':')
        if key in ('MemTotal', 'MemAvailable', 'SwapTotal', 'SwapFree'):
            values[key + '_kib'] = int(value.split()[0])
    return values


def cgroup():
    relative = read('/proc/self/cgroup', '').split('::')[-1]
    base = Path('/sys/fs/cgroup') / relative.lstrip('/')
    return {'path': relative,
            'cpu.max': read(base / 'cpu.max', 'not present (no CPU quota controller)'),
            'cpuset.cpus.effective': read(base / 'cpuset.cpus.effective', 'not present'),
            'memory.max': read(base / 'memory.max', 'not present')}


def compiler_target():
    """GCC's default target options: no -march flag is passed to either build."""
    options = {}
    for line in (command('gcc', '-Q', '--help=target') or '').splitlines():
        match = re.match(r'\s+-m(arch|tune|cpu)=\s+(\S+)\s*$', line)
        if match:
            options.setdefault(match.group(1), match.group(2))
    return {'machine': command('gcc', '-dumpmachine'), **options}


def ci():
    """The hosted runner, when GitHub Actions runs the campaign."""
    if os.environ.get('GITHUB_ACTIONS') != 'true':
        return None
    return {'service': 'GitHub Actions',
            **{key.lower(): os.environ.get(key) for key in (
                'RUNNER_ENVIRONMENT', 'RUNNER_ARCH', 'RUNNER_NAME', 'ImageOS', 'ImageVersion',
                'GITHUB_REPOSITORY', 'GITHUB_REF_NAME', 'GITHUB_SHA', 'GITHUB_RUN_ID')}}


def main():
    info = lscpu()
    # x86-64 lists "flags", AArch64 "Features".
    match = re.search(r'^(?:flags|Features)\s*:\s*(.*)$', read('/proc/cpuinfo', ''), re.M)
    flags = set(match.group(1).split()) if match else set()
    topology = cpu_topology()
    cores = {(c['package_id'], c['core_id']) for c in topology}
    environment = {
        'recorded': datetime.datetime.now(datetime.UTC).isoformat(timespec='seconds'),
        'cpu': {
            'model': info.get('Model name'),
            'vendor': info.get('Vendor ID'),
            'family_model_stepping': f"{info.get('CPU family')}/{info.get('Model')}/{info.get('Stepping')}",
            'sockets': integer(info.get('Socket(s)'), 1),
            'physical_cores': len(cores),
            'logical_cpus': integer(info.get('CPU(s)'), os.cpu_count()),
            'threads_per_core': integer(info.get('Thread(s) per core'), 1),
            'available_to_benchmark': len(topology),
            'affinity': sorted(os.sched_getaffinity(0)),
            'smt_active': read('/sys/devices/system/cpu/smt/active'),
            'smt_control': read('/sys/devices/system/cpu/smt/control'),
            'max_mhz': info.get('CPU max MHz'),
            'min_mhz': info.get('CPU min MHz'),
            'boost': read('/sys/devices/system/cpu/cpufreq/boost'),
            'amd_pstate': read('/sys/devices/system/cpu/amd_pstate/status'),
            'caches': {k: info.get(k) for k in ('L1d cache', 'L1i cache', 'L2 cache', 'L3 cache')},
            'numa_nodes': info.get('NUMA node(s)'),
            'instruction_sets': sorted(f for f in FLAGS if f in flags),
            'topology': topology,
        },
        'memory': meminfo(),
        'transparent_hugepage': {
            'enabled': read('/sys/kernel/mm/transparent_hugepage/enabled'),
            'defrag': read('/sys/kernel/mm/transparent_hugepage/defrag'),
        },
        'os': {
            'pretty_name': re.search(r'PRETTY_NAME="([^"]+)"', read('/etc/os-release', '')).group(1),
            'kernel': platform.release(),
            'kernel_version': platform.version(),
            'architecture': platform.machine(),
            'clocksource': read('/sys/devices/system/clocksource/clocksource0/current_clocksource'),
            'perf_event_paranoid': read('/proc/sys/kernel/perf_event_paranoid'),
            'virtualization': command('systemd-detect-virt') or 'unknown',
            'cgroup': cgroup(),
        },
        'toolchain': {
            'gcc': command('gcc', '--version').splitlines()[0],
            'clang': (command('clang', '--version') or '').splitlines()[0:1],
            'cmake': command('cmake', '--version').splitlines()[0],
            'ninja': command('ninja', '--version'),
            'glibc': command('ldd', '--version').splitlines()[0],
            'python': platform.python_version(),
            'openssl': command('openssl', 'version'),
            'gcc_target': compiler_target(),
        },
        'ci': ci(),
        'files': {},
    }
    for path in sorted(WORK.glob('bin/bench-*')) + sorted(WORK.glob('build/*-release/libmaxminddb.a')) + sorted(WORK.glob('data/*.mmdb')):
        environment['files'][str(path.relative_to(WORK))] = {'sha256': sha256(path), 'bytes': path.stat().st_size}
    print(json.dumps(environment, indent=2))


if __name__ == '__main__':
    main()
