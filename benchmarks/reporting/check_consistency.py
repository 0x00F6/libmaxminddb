#!/usr/bin/env python3
"""Cross-check that every deliverable reports the numbers in summary.json."""
import argparse
import html
import json
import re
import struct
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import report  # noqa: E402
from charts import fmt_mlps  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
REPORTS = ROOT / 'benchmarks/reports'
failures = []


def check(name, ok, detail=''):
    print(f'{"PASS" if ok else "FAIL"} {name}{" — " + detail if detail and not ok else ""}')
    if not ok:
        failures.append(name)


def main():
    global REPORTS
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reports', default=str(REPORTS), help='directory holding the generated reports')
    REPORTS = Path(parser.parse_args().reports)
    summary = json.loads((REPORTS / 'performance-comparison-data/summary.json').read_text())
    report.configure(summary)
    ctx = report.Context(summary)
    pr = (REPORTS / 'pull-request-description.md').read_text()
    page = (REPORTS / 'performance-comparison.html').read_text()
    manifest = json.loads((REPORTS / 'performance-comparison-assets/manifest.json').read_text())
    cpu = ctx.cpu

    # The PR shows the charts instead of result tables.
    benchmarks = pr.split('## 📊 Benchmarks', 1)[1].split('\n## ', 1)[0]
    tables = re.findall(r'(?:^\|.*\n)+', benchmarks, re.M)
    check('PR benchmarks section has no result tables', len(tables) == 1 and '| CPU |' in tables[0],
          f'{len(tables)} tables')
    images = re.findall(r'!\[[^\]]*\]\(([^)\s]+)\)', pr)
    for target in images:
        check(f'PR image {target} exists', target.endswith('.svg') and (REPORTS / target).is_file())
    check('PR shows throughput and candlestick charts', any('/throughput-' in t for t in images)
          and any('/candlestick-' in t for t in images))

    # HTML throughput table values equal the summary medians.
    table = page.split('id="throughput-table"', 1)[1].split('</table>', 1)[0]
    rows = re.findall(r'<tr data-db="(\w+)" data-profile="(\w+)" data-op="(\w+)" data-threads="(\d+)"[^>]*>(.*?)</tr>', table)
    index = {}
    for db, profile, op, threads, cells in rows:
        values = re.findall(r'<td[^>]*>(.*?)</td>', cells)
        data_v = re.findall(r'data-v="([^"]*)"', cells)
        if len(values) < 10 or not values[3].startswith('IPv'):
            continue
        family = int(values[3][3])
        size = int(data_v[0])
        index[(db, profile, op, family, size, int(threads))] = [float(x) for x in data_v[2:5]]
    matched = 0
    for c in summary['throughput']:
        key = (c['database'], c['profile'], c['operation'], c['family'], c['queries'], c['threads'])
        got = index.get(key)
        want = [c['before']['throughput_mlps']['median'], c['after']['throughput_mlps']['median'],
                c['comparison']['speedup']]
        if got and all(abs(a - b) <= 1e-5 * max(1, abs(b)) for a, b in zip(got, want)):
            matched += 1
    check('HTML throughput table equals summary', matched == len(summary['throughput']),
          f'{matched}/{len(summary["throughput"])}')

    # The After commits form one chain on top of Before, with the source tree that was built and measured.
    def git(*args):
        return subprocess.run(['git', '-C', str(ROOT), *args], capture_output=True, text=True).stdout.strip()
    after = summary['after']
    commits = [git('rev-parse', '--verify', '--quiet', f'{c}^{{commit}}') for c in after['commits']]
    chain = [summary['before']['commit']] + commits
    check('After commits form a chain from Before to After', all(commits) and commits[-1] == after['commit']
          and all(git('rev-parse', f'{child}^') == parent for parent, child in zip(chain, chain[1:])))
    check('After and the commit its executables were built from have the recorded tree',
          git('rev-parse', f'{after["commit"]}^{{tree}}') == after['tree']
          == git('rev-parse', f'{after["built_from"]}^{{tree}}'))

    # Title percentage.
    expected = round(summary['headline']['throughput']['city']['geomean_change_percent'])
    check('PR title percentage', pr.startswith(f'# Improve performance +{expected}%'), pr.splitlines()[0])
    check('CPU model in PR benchmarks section', cpu in pr.split('## 📊 Benchmarks', 1)[1])
    check('CPU model in HTML title and header', f'<title>libmaxminddb Before/After Performance — {html.escape(cpu)}' in page
          and html.escape(cpu) in page.split('<h1>', 1)[1].split('</h1>', 1)[0])

    # Exported figures: labels, legends, CPU footer and PNG pixel sizes.
    for item in manifest:
        svg = (REPORTS / item['svg']).read_text()
        text = html.unescape(re.sub(r'<[^>]+>', ' ', svg))
        ok = (cpu in text and f'Before · {ctx.before_short}' in text and f'After · {ctx.after_short}' in text
              and ('Higher is better' in text or 'Lower is better' in text))
        if item['kind'] == 'diagram':
            # The radix-16 diagram has no axes or legend; it shows the campaign's database and CPU.
            ok = (cpu in text and f'{ctx.databases["city"]["node_count"]:,} nodes' in text
                  and f'Before {ctx.before_short}' in text and f'After {ctx.after_short}' in text)
        if item['kind'] == 'candlestick':
            ok = ok and 'Lower whisker: min → p50' in text and 'max ' in text
        check(f'{item["id"]}.svg labels, legend and CPU', ok)
        png = REPORTS / item['png']
        if png.exists():
            width, height = struct.unpack('>II', png.read_bytes()[16:24])
            check(f'{item["id"]}.png size', (width, height) == tuple(item['png_size']) and 1600 <= width <= 2400,
                  f'{width}x{height}')
        else:
            check(f'{item["id"]}.png exists', False)
        if item['kind'] == 'throughput':
            configs = ctx.select('throughput', database=item.get('database', 'city'), profile=item.get('profile', 'random'),
                                 operation=item.get('operation', 'lookup'), family=item['family'],
                                 queries=item['size'])
            labels = [f'{fmt_mlps(c[v]["throughput_mlps"]["median"])} M/s' for c in configs for v in ('before', 'after')]
            check(f'{item["id"]}.svg bar labels equal summary', all(label in text for label in labels))
        if f']({item["svg"]})' in pr:
            check(f'{item["id"]}.svg embedded in PR with alt text and caption',
                  f'![{item["alt"]}]({item["svg"]})\n\n*{item["caption"]}*' in pr)
    print(f'{len(failures)} failure(s)')
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())
