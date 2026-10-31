#!/usr/bin/env python3
"""Write the index pages of the site that pages.sh builds: a landing page at
the root with one card per architecture, and a file listing in every directory
that has no index.html, so the published figures and data can be browsed. The
reports live under <After commit>/: <commit>/<architecture>/ and the merged
<commit>/performance-comparison-<branch>.html.

Usage: python3 benchmarks/reporting/pages.py SITE_DIRECTORY
DATABASE_SOURCE, when set, names where GeoLite2-City was downloaded from.
"""
import datetime
import html
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from charts import THEME, fmt_pct, fmt_speedup  # noqa: E402

ARCH_LABEL = {'x86_64': 'x86-64', 'aarch64': 'AArch64'}
CSS = f'''
:root{{color-scheme:dark}}
*{{box-sizing:border-box}}
body{{margin:0;background:#080e12;color:{THEME["text"]};font:15px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif}}
.wrap{{max-width:1080px;margin:0 auto;padding:40px 24px 64px}}
h1{{font-size:clamp(26px,3.6vw,40px);line-height:1.15;margin:0 0 10px;letter-spacing:-.01em}}
h2{{font-size:20px;margin:0}}
p{{color:{THEME["text2"]};margin:8px 0}}
a{{color:{THEME["accent"]}}}
a:focus-visible{{outline:2px solid {THEME["accent"]};outline-offset:2px}}
code{{font:13px ui-monospace,SFMono-Regular,Menlo,monospace;color:{THEME["text2"]}}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:16px;margin:28px 0}}
.card{{background:{THEME["surface"]};border:1px solid {THEME["line"]};border-radius:12px;padding:20px 22px}}
.label{{color:{THEME["muted"]};font-size:12px;text-transform:uppercase;letter-spacing:.06em}}
.value{{font-size:34px;font-weight:700;margin:4px 0 0}}
dl{{display:grid;grid-template-columns:auto 1fr;gap:4px 14px;margin:16px 0;font-size:14px}}
dt{{color:{THEME["muted"]}}}
dd{{margin:0;color:{THEME["text2"]};overflow-wrap:anywhere}}
ul.links{{list-style:none;padding:0;margin:14px 0 0;display:flex;flex-wrap:wrap;gap:8px}}
ul.links a{{display:inline-block;padding:6px 12px;border:1px solid {THEME["line"]};border-radius:8px;text-decoration:none}}
ul.links a:hover{{background:{THEME["accent_bg"]}}}
table{{border-collapse:collapse;width:100%;font-size:14px}}
th,td{{text-align:left;padding:7px 10px;border-bottom:1px solid {THEME["line"]}}}
th{{color:{THEME["muted"]};font-weight:600}}
.n{{text-align:right;font-variant-numeric:tabular-nums}}
footer{{margin-top:36px;color:{THEME["muted"]};font-size:13px}}
'''


def esc(value):
    return html.escape(str(value), quote=True)


def page(title, body):
    return (f'<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1"><title>{esc(title)}</title>'
            f'<style>{CSS}</style></head><body><div class="wrap">{body}</div></body></html>\n')


def card(directory, summary):
    env = summary['environment']
    cpu, ci = env['cpu'], env.get('ci') or {}
    arch = ARCH_LABEL.get(env['os']['architecture'], env['os']['architecture'])
    city = summary['headline']['throughput']['city']
    latency = (summary['headline'].get('latency') or {}).get('city')
    database = summary['databases']['city']
    rows = [
        ('CPU', f'{cpu["model"]} · {cpu["physical_cores"]} cores / {cpu["logical_cpus"]} threads'),
        ('Machine', f'{ci.get("service", "")} runner, image {ci.get("imageos")} {ci.get("imageversion")}'
         if ci else env['os']['virtualization']),
        ('Throughput', f'{fmt_pct(city["geomean_change_percent"])} geometric mean of {city["configurations"]} GeoLite2-City '
                       f'configurations · {city["faster"]} faster, {city["slower"]} slower, {city["inconclusive"]} inconclusive'),
    ] + ([
        ('p99 latency', f'lower in {latency["p99"]["improved"]} of {latency["configurations"]} configurations'),
    ] if latency else []) + [
        ('GeoLite2-City', f'build {database["build_date"]}, SHA-256 {database["sha256"][:12]}…'),
        ('Recorded', env['recorded']),
    ]
    links = [('performance-comparison.html', 'Interactive report'),
             ('performance-comparison-assets/', 'Figures (SVG, PNG)'),
             ('performance-comparison-data/', 'Data (JSON, CSV)'),
             ('performance-comparison-data/summary.json', 'summary.json'),
             ('pull-request-description.md', 'Pull request draft')]
    name = directory.rsplit('/', 1)[-1]
    return (f'<section class="card" aria-labelledby="{esc(name)}">'
            f'<div class="label">{esc(name)}</div><h2 id="{esc(name)}">{esc(arch)}</h2>'
            f'<div class="value">{esc(fmt_speedup(city["geomean_speedup"]))}</div>'
            '<p>After / Before throughput, geometric mean</p>'
            '<dl>' + ''.join(f'<dt>{esc(k)}</dt><dd>{esc(v)}</dd>' for k, v in rows) + '</dl>'
            '<ul class="links">' + ''.join(f'<li><a href="{esc(directory)}/{esc(href)}">{esc(text)}</a></li>'
                                           for href, text in links) + '</ul></section>')


def landing(site, reports):
    first = reports[0][1]
    before, after = first['before'], first['after']
    merged = sorted(site.glob('*/performance-comparison-*.html'))
    source = os.environ.get('DATABASE_SOURCE')
    shas = {summary['databases']['city']['sha256'] for _, summary in reports}
    same_db = ('Every architecture measured the same GeoLite2-City file.' if len(shas) == 1
               else 'The architectures measured different GeoLite2-City files; compare them with care.')
    body = ('<header><div class="label">libmaxminddb · Before/After performance</div>'
            '<h1>Lookup performance on every measured architecture</h1>'
            f'<p>Before <code>{esc(before["commit"])}</code> (upstream main) versus After '
            f'<code>{esc(after["commit"])}</code>, built and measured by the same harness on each machine. '
            'Each report links its figures and the per-run data it was generated from.</p>'
            + ''.join(f'<ul class="links"><li><a href="{esc(m.relative_to(site))}">All architectures on one page '
                      f'({esc(m.name)})</a></li></ul>' for m in merged) + '</header>'
            '<div class="cards">' + ''.join(card(d, s) for d, s in reports) + '</div>'
            f'<p>{esc(same_db)}'
            + (f' Downloaded from <a href="{esc(source)}">{esc(source)}</a>.' if source else '') + '</p>'
            '<footer>GeoLite2 data created by MaxMind, available from '
            '<a href="https://www.maxmind.com">https://www.maxmind.com</a>. '
            f'Generated {esc(datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"))} by '
            '<code>benchmarks/reporting/pages.py</code>.</footer>')
    (site / 'index.html').write_text(page('libmaxminddb Before/After performance', body))


def listing(site, directory):
    """A file listing for a directory without its own index page."""
    relative = directory.relative_to(site)
    entries = sorted(directory.iterdir(), key=lambda p: (not p.is_dir(), p.name))
    rows = ''.join(
        f'<tr><td><a href="{esc(p.name)}{"/" if p.is_dir() else ""}">{esc(p.name)}{"/" if p.is_dir() else ""}</a></td>'
        f'<td class="n">{"" if p.is_dir() else f"{p.stat().st_size:,}"}</td></tr>'
        for p in entries)
    body = (f'<header><div class="label">libmaxminddb · Before/After performance</div><h1>/{esc(relative)}/</h1>'
            '<p><a href="../">Parent directory</a></p></header>'
            f'<table><thead><tr><th scope="col">Name</th><th scope="col" class="n">Bytes</th></tr></thead>'
            f'<tbody>{rows}</tbody></table>')
    (directory / 'index.html').write_text(page(f'/{relative}/', body))


def main():
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    site = Path(sys.argv[1])
    reports = [(str(p.parent.parent.relative_to(site)), json.loads(p.read_text()))
               for p in sorted(site.glob('*/*/performance-comparison-data/summary.json'))]
    if not reports:
        raise SystemExit(f'no report in {site}')
    for directory in sorted(p for p in site.rglob('*') if p.is_dir()):
        if not (directory / 'index.html').exists():
            listing(site, directory)
    landing(site, reports)
    print(f'Index pages written for {", ".join(d for d, _ in reports)} in {site}')


if __name__ == '__main__':
    main()
