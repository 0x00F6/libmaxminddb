"""Dependency-free SVG charts for the benchmark report.

The same functions render the charts embedded in the HTML report and the
standalone exports. Standalone charts carry their title, configuration,
legend, axis labels, direction of improvement and the measuring CPU, so an
exported image can be read without the report.
"""
import html
import json
import math
import re

THEME = {
    'page': '#080e12', 'surface': '#10191f', 'surface2': '#142127',
    'grid': '#1d2a32', 'axis': '#34454f', 'line': '#223039',
    'text': '#eef3f5', 'text2': '#c5d0d6', 'muted': '#94a5ae',
    'accent': '#55f1b1', 'accent_bg': '#14372c',
    # Validated categorical pair (dataviz dark slots 1 and 2) on #10191f.
    'before': '#3987e5', 'after': '#d95926',
    'good': '#0ca30c', 'critical': '#e66767', 'neutral': '#94a5ae',
}
SANS = "Inter, 'Segoe UI', 'Noto Sans', 'Helvetica Neue', Arial, sans-serif"
MONO = "'SFMono-Regular', Consolas, 'Noto Sans Mono', 'DejaVu Sans Mono', monospace"
VERSION_LABEL = {'before': 'Before', 'after': 'After'}


def esc(value):
    return html.escape(str(value), quote=True)


def text_width(text, size, mono=False):
    return len(str(text)) * size * (0.61 if mono else 0.56)


def nice_step(span, target=5):
    if span <= 0:
        return 1
    raw = span / target
    power = 10 ** math.floor(math.log10(raw))
    for factor in (1, 2, 2.5, 5, 10):
        if raw <= factor * power:
            return factor * power
    return 10 * power


def nice_max(value, target=5):
    step = nice_step(value, target)
    return math.ceil(value / step) * step, step


def fmt_number(value, digits=2):
    if value is None:
        return 'n/a'
    return f'{value:,.{digits}f}'


def fmt_mlps(value):
    if value is None:
        return 'n/a'
    return f'{value:,.1f}' if value >= 100 else f'{value:,.2f}'


def fmt_ns(value):
    if value is None:
        return 'n/a'
    if value >= 1_000_000:
        return f'{value / 1e6:,.2f} ms'
    if value >= 10_000:
        return f'{value / 1000:,.1f} µs'
    if value >= 1000:
        return f'{value / 1000:,.2f} µs'
    return f'{value:,.0f} ns'


def fmt_ms(value):
    if value is None:
        return 'n/a'
    if value < 1:
        return f'{value:.3f} ms'
    if value < 10:
        return f'{value:.2f} ms'
    return f'{value:,.1f} ms'


def fmt_mib(value):
    return 'n/a' if value is None else f'{value:,.1f} MiB'


def fmt_pct(value, digits=1):
    if value is None:
        return 'n/a'
    sign = '+' if value > 0 else '−' if value < 0 else '±'
    return f'{sign}{abs(value):,.{digits}f}%'


def fmt_speedup(value):
    return 'n/a' if value is None else f'×{value:,.2f}'


def tick_label(value, step):
    if step >= 1:
        return f'{value:,.0f}'
    digits = max(0, -math.floor(math.log10(step)) + (1 if step * 10 ** -math.floor(math.log10(step)) == 2.5 else 0))
    return f'{value:,.{digits}f}'


class Svg:
    def __init__(self, width, height, label):
        self.width, self.height = width, height
        self.parts = []
        self.label = label

    def add(self, fragment):
        self.parts.append(fragment)

    def text(self, x, y, content, size=13, color=None, anchor='start', weight=400,
             mono=False, extra=''):
        self.add(f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size}" '
                 f'font-family="{esc(MONO if mono else SANS)}" fill="{color or THEME["text"]}" '
                 f'text-anchor="{anchor}" font-weight="{weight}"{extra}>{esc(content)}</text>')

    def line(self, x1, y1, x2, y2, color, width=1, extra=''):
        self.add(f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" '
                 f'stroke="{color}" stroke-width="{width}"{extra}/>')

    def rect(self, x, y, w, h, color, rx=0, extra=''):
        self.add(f'<rect x="{x:.1f}" y="{y:.1f}" width="{max(0, w):.1f}" height="{h:.1f}" '
                 f'rx="{rx}" fill="{color}"{extra}/>')

    def bar(self, x0, y, x1, h, color, extra=''):
        """Horizontal bar, square at the baseline, 4px rounded data end."""
        width = x1 - x0
        r = min(4, width / 2, h / 2)
        if width <= 0.5:
            self.line(x0, y, x0, y + h, color, 1.5, extra)
            return
        self.add(f'<path d="M{x0:.1f},{y:.1f} H{x1 - r:.1f} A{r:.1f},{r:.1f} 0 0 1 {x1:.1f},{y + r:.1f} '
                 f'V{y + h - r:.1f} A{r:.1f},{r:.1f} 0 0 1 {x1 - r:.1f},{y + h:.1f} H{x0:.1f} Z" '
                 f'fill="{color}"{extra}/>')

    def render(self, background=True, standalone=True):
        size = (f'width="{self.width}" height="{self.height}" ' if standalone else '')
        bg = (f'<rect width="{self.width}" height="{self.height}" fill="{THEME["surface"]}"/>'
              if background else '')
        return (f'<svg xmlns="http://www.w3.org/2000/svg" {size}viewBox="0 0 {self.width} {self.height}" '
                f'role="{'img' if standalone else 'group'}" aria-label="{esc(self.label)}" class="chart" '
                f'preserveAspectRatio="xMinYMin meet" style="font-family:{esc(SANS)}">'
                f'<title>{esc(self.label)}</title>{bg}{"".join(self.parts)}</svg>')


def tip(rows):
    """Tooltip payload (ordered [label, value] pairs, inserted with textContent)
    plus keyboard focus and an accessible name for the mark."""
    label = '; '.join(f'{a}: {b}' for a, b in rows[:6])
    return (f' data-tip="{esc(json.dumps(rows, ensure_ascii=False))}" tabindex="0" role="img" '
            f'aria-label="{esc(label)}"')


def direction_pill(svg, x_right, y, higher):
    label = '↑ Higher is better' if higher else '↓ Lower is better'
    width = text_width(label, 12, mono=True) + 24
    svg.rect(x_right - width, y - 15, width, 24, THEME['accent_bg'], rx=6,
             extra=f' stroke="{THEME["accent"]}" stroke-opacity="0.45"')
    svg.text(x_right - width / 2, y + 1.5, label, 12, THEME['accent'], 'middle', 600, mono=True)


def version_legend(svg, x, y, versions_meta):
    cursor = x
    for version in ('before', 'after'):
        svg.rect(cursor, y - 10, 14, 14, THEME[version], rx=3)
        label = f'{VERSION_LABEL[version]} · {versions_meta[version]}'
        svg.text(cursor + 22, y + 1.5, label, 13, THEME['text2'])
        cursor += 22 + text_width(label, 13) + 28
    return cursor


def header(svg, meta, title, subtitle, higher, pad=32):
    if meta['standalone']:
        svg.text(pad, 46, title, 24, THEME['text'], weight=700)
        svg.text(pad, 72, subtitle, 14, THEME['text2'])
        direction_pill(svg, svg.width - pad, 42, higher)
        y = 104
    else:
        direction_pill(svg, svg.width - pad, 26, higher)
        y = 30
    return y


def wrap_footer(lines, width, size=12):
    """Greedily pack ' · '-separated segments so no footer line overflows."""
    out = []
    for line in lines:
        current = ''
        for segment in line.split(' · '):
            candidate = f'{current} · {segment}' if current else segment
            if current and text_width(candidate, size) > width:
                out.append(current)
                current = segment
            else:
                current = candidate
        out.append(current)
    # Sentence-case continuation lines, but never paths or identifiers.
    return [line[:1].upper() + line[1:] if re.match(r'[a-z]+[ ,]', line) else line for line in out]


def footer(svg, meta, lines, pad=32):
    y = svg.height - 18 - 18 * (len(lines) - 1)
    svg.line(pad, y - 22, svg.width - pad, y - 22, THEME['line'])
    for line in lines:
        svg.text(pad, y, line, 12, THEME['muted'])
        y += 18


def x_axis(svg, x0, x1, y, maximum, step, scale, title, minor_grid_top=None):
    value = 0.0
    while value <= maximum + step * 1e-9:
        x = scale(value)
        if minor_grid_top is not None:
            svg.line(x, minor_grid_top, x, y, THEME['grid'])
        svg.text(x, y + 20, tick_label(value, step), 12, THEME['muted'], 'middle', mono=True)
        value += step
    svg.line(x0, y, x1, y, THEME['axis'])
    svg.text((x0 + x1) / 2, y + 44, title, 13, THEME['text2'], 'middle')


def badge(svg, x, y, speedup, low, high, change, size=16, kind='throughput'):
    """Speedup annotation with an icon so direction never relies on color."""
    if low is not None and low > 1:
        icon, color, word = '▲', THEME['good'], 'faster'
    elif high is not None and high < 1:
        icon, color, word = '▼', THEME['critical'], 'slower'
    else:
        icon, color, word = '≈', THEME['neutral'], 'inconclusive'
    svg.text(x, y, icon, size - 2, color, weight=700)
    svg.text(x + 20, y, f'{fmt_speedup(speedup)}  {fmt_pct(change)}', size, THEME['text'], weight=700)
    ci = f'95% CI ×{low:.2f}–{high:.2f} · {word}' if low is not None else word
    svg.text(x + 20, y + 18, ci, 12, THEME['muted'])


def throughput_chart(meta, configs, title, subtitle, footer_lines):
    """Grouped horizontal bars: Before/After at every worker count."""
    footer_lines = wrap_footer(footer_lines, 1100 - 64)
    standalone = meta['standalone']
    W, pad = 1100, 32
    label_w, right_w = 190, 250
    x0 = pad + label_w + 26
    x1 = W - pad - right_w
    bar_h, inner, group_gap = 22, 2, 30
    groups = len(configs)
    plot_h = groups * (2 * bar_h + inner) + (groups - 1) * group_gap
    top = (104 if standalone else 52) + 34
    axis_y = top + plot_h + 14
    H = axis_y + 62 + (18 * len(footer_lines) + 30 if standalone else 0)
    svg = Svg(W, H, f'{title}. {subtitle}')
    header(svg, meta, title, subtitle, True)
    version_legend(svg, pad, top - 22, meta['versions'])
    svg.text(x1 + 24, top - 20.5, 'Speedup (After / Before)', 12, THEME['muted'])
    high_values = []
    for c in configs:
        for v in ('before', 'after'):
            t = c[v]['throughput_mlps']
            high_values.append(max(t['median'], t['ci95_high'] or 0))
    maximum, step = nice_max(max(high_values) * 1.16)
    scale = lambda value: x0 + (x1 - x0) * value / maximum
    # Gridlines first so marks sit above them.
    value = 0.0
    while value <= maximum + 1e-9:
        svg.line(scale(value), top - 6, scale(value), axis_y, THEME['grid'])
        value += step
    y = top
    for c in configs:
        threads = c['threads']
        svg.add(f'<g data-threads="{threads}" class="group">')
        group_mid = y + bar_h + inner / 2
        svg.text(pad, group_mid - 4, f'{threads} thread{"s" if threads > 1 else ""}', 15, THEME['text'], weight=600)
        svg.text(pad, group_mid + 14, f'{c["queries"] // threads:,} per worker', 11.5, THEME['muted'])
        for index, version in enumerate(('before', 'after')):
            t = c[version]['throughput_mlps']
            by = y + index * (bar_h + inner)
            svg.text(x0 - 10, by + bar_h / 2 + 4.5, VERSION_LABEL[version], 12, THEME['muted'], 'end')
            rows = [[f'{VERSION_LABEL[version]} · {threads} thread{"s" if threads > 1 else ""}', f'{fmt_mlps(t["median"])} M lookups/s'],
                    ['Median of runs', str(t['n'])],
                    ['95% CI of median', f'{fmt_mlps(t["ci95_low"])} – {fmt_mlps(t["ci95_high"])}'],
                    ['Min – max', f'{fmt_mlps(t["min"])} – {fmt_mlps(t["max"])}'],
                    ['CV', f'{t["cv_percent"]:.1f}%'],
                    ['Workload duration (median)', fmt_ms(c[version]['elapsed_ms']['median'])]]
            cu = c[version].get('cpu_utilization_percent')
            if cu:
                rows.append(['CPU utilization', f'{cu["median"]:.1f}%'])
            cy = c[version].get('core_cycles_per_lookup')
            if cy:
                rows.append(['Core cycles per lookup (APERF)', f'{cy["median"]:.0f}'])
            svg.bar(x0, by, scale(t['median']), bar_h, THEME[version],
                    extra=f' class="mark"{tip(rows)}')
            if t['ci95_low'] is not None:
                cl, ch = scale(t['ci95_low']), scale(t['ci95_high'])
                mid = by + bar_h / 2
                svg.line(cl, mid, ch, mid, THEME['text'], 1.5, ' stroke-opacity="0.85"')
                svg.line(cl, mid - 5, cl, mid + 5, THEME['text'], 1.5, ' stroke-opacity="0.85"')
                svg.line(ch, mid - 5, ch, mid + 5, THEME['text'], 1.5, ' stroke-opacity="0.85"')
            end = max(scale(t['median']), scale(t['ci95_high'] or t['median']))
            svg.text(end + 8, by + bar_h / 2 + 4.5, f'{fmt_mlps(t["median"])} M/s', 12.5, THEME['text'], mono=True)
        comp = c['comparison']
        badge(svg, x1 + 24, group_mid + 1, comp['speedup'], comp['speedup_ci95_low'],
              comp['speedup_ci95_high'], comp['change_percent'])
        svg.add('</g>')
        y += 2 * bar_h + inner + group_gap
    x_axis(svg, x0, x1, axis_y, maximum, step, scale,
           'Aggregate throughput (M lookups/s, higher is better)')
    if standalone:
        footer(svg, meta, footer_lines)
    return svg.render(standalone=standalone)


def candlestick_glyph_legend(svg, x, y):
    """Explains the convention; drawn in every candlestick chart."""
    g = THEME['text2']
    svg.line(x, y, x + 40, y, g, 2)
    svg.line(x, y - 5, x, y + 5, g, 2)
    svg.rect(x + 40, y - 7, 46, 14, g, rx=2, extra=' fill-opacity="0.55"')
    svg.line(x + 86, y, x + 120, y, g, 2)
    svg.line(x + 120, y - 5, x + 120, y + 5, g, 2)
    svg.add(f'<path d="M{x + 40},{y - 6} l6,6 l-6,6 l-6,-6 Z" fill="{THEME["text"]}" '
            f'stroke="{THEME["surface"]}" stroke-width="2"/>')
    for offset, label in ((0, 'min'), (40, 'p50'), (86, 'p95'), (120, 'p99')):
        svg.text(x + offset, y + 22, label, 11, THEME['muted'], 'middle', mono=True)
    svg.text(x + 140, y - 2, 'Lower whisker: min → p50 · body: p50 → p95 · upper whisker: p95 → p99', 12, THEME['text2'])
    svg.text(x + 140, y + 15, '◆ marks p50 · maximum listed at the right of each row (off-scale)', 12, THEME['text2'])


def candlestick_chart(meta, configs, title, subtitle, footer_lines):
    footer_lines = wrap_footer(footer_lines, 1100 - 64)
    standalone = meta['standalone']
    W, pad = 1100, 32
    label_w, right_w = 205, 236
    x0 = pad + label_w + 20
    x1 = W - pad - right_w
    row_h, group_gap = 30, 22
    groups = len(configs)
    plot_h = groups * 2 * row_h + (groups - 1) * group_gap
    top = (104 if standalone else 52) + 78
    axis_y = top + plot_h + 10
    H = axis_y + 62 + (18 * len(footer_lines) + 30 if standalone else 0)
    svg = Svg(W, H, f'{title}. {subtitle}')
    header(svg, meta, title, subtitle, False)
    legend_y = top - 66
    version_legend(svg, pad, legend_y, meta['versions'])
    candlestick_glyph_legend(svg, pad + 8, legend_y + 34)
    svg.text(x1 + 22, top - 8, 'p99 · max (sampled)', 12, THEME['muted'])
    p99s = [c[v]['latency_pooled_ns']['p99'] for c in configs for v in ('before', 'after')]
    maximum, step = nice_max(max(p99s) * 1.06)
    scale = lambda value: x0 + (x1 - x0) * min(value, maximum) / maximum
    value = 0.0
    while value <= maximum + 1e-9:
        svg.line(scale(value), top - 4, scale(value), axis_y, THEME['grid'])
        value += step
    y = top
    for c in configs:
        threads = c['threads']
        svg.add(f'<g data-threads="{threads}" class="group">')
        lat = c['latency_comparison']
        svg.text(pad, y + row_h - 2, f'{threads} thread{"s" if threads > 1 else ""}', 15, THEME['text'], weight=600)
        p50r, p99r = lat['p50']['reduction_percent'], lat['p99']['reduction_percent']
        icon, color = (('▼', THEME['good']) if (p99r or 0) > 0 else ('▲', THEME['critical']) if (p99r or 0) < 0 else ('≈', THEME['neutral']))
        svg.text(pad, y + row_h + 17, icon, 11, color, weight=700)
        svg.text(pad + 14, y + row_h + 17, f'p50 {fmt_pct(-p50r, 0)} · p99 {fmt_pct(-p99r, 0)}', 11.5, THEME['text2'])
        for index, version in enumerate(('before', 'after')):
            q = c[version]['latency_pooled_ns']
            ry = y + index * row_h
            mid = ry + row_h / 2
            color = THEME[version]
            svg.text(x0 - 12, mid + 4.5, VERSION_LABEL[version], 12, THEME['muted'], 'end')
            rows = [[f'{VERSION_LABEL[version]} · {threads} thread{"s" if threads > 1 else ""}', f'p99 {fmt_ns(q["p99"])}'],
                    ['min', fmt_ns(q['min'])], ['p50', fmt_ns(q['p50'])], ['p90', fmt_ns(q['p90'])],
                    ['p95', fmt_ns(q['p95'])], ['p99', fmt_ns(q['p99'])], ['p99.9', fmt_ns(q['p999'])],
                    ['max', fmt_ns(q['max'])], ['mean', fmt_ns(q['mean'])],
                    ['Sampled observations', f'{q["observations"]:,}']]
            svg.add(f'<g class="mark"{tip(rows)}>')
            svg.rect(x0, ry + 1, x1 - x0, row_h - 2, THEME['surface'], extra=' fill-opacity="0"')
            xmin, x50, x95, x99 = (scale(q[k]) for k in ('min', 'p50', 'p95', 'p99'))
            svg.line(xmin, mid, x50, mid, color, 2)
            svg.line(xmin, mid - 6, xmin, mid + 6, color, 2)
            svg.rect(x50, mid - 8, max(2, x95 - x50), 16, color, rx=2)
            svg.line(x95, mid, x99, mid, color, 2)
            svg.line(x99, mid - 6, x99, mid + 6, color, 2)
            svg.add(f'<path d="M{x50:.1f},{mid - 7:.1f} l7,7 l-7,7 l-7,-7 Z" fill="{THEME["text"]}" '
                    f'stroke="{THEME["surface"]}" stroke-width="2"/>')
            svg.add('</g>')
            svg.text(x1 + 22, mid + 4.5, f'{fmt_ns(q["p99"])}', 12.5, THEME['text'], mono=True)
            svg.text(x1 + 22 + 92, mid + 4.5, f'max {fmt_ns(q["max"])}', 12, THEME['muted'], mono=True)
        svg.add('</g>')
        y += 2 * row_h + group_gap
    x_axis(svg, x0, x1, axis_y, maximum, step, scale,
           'Sampled per-lookup latency (ns, linear scale, lower is better)')
    if standalone:
        footer(svg, meta, footer_lines)
    return svg.render(standalone=standalone)


def scaling_chart(meta, panels, title, subtitle, footer_lines):
    """Small multiples of throughput(N) / throughput(1) with the ideal line."""
    footer_lines = wrap_footer(footer_lines, 1100 - 64)
    standalone = meta['standalone']
    W, pad = 1100, 32
    cols = 2
    rows_n = math.ceil(len(panels) / cols)
    panel_w, panel_h = (W - 2 * pad - 40) / cols, 250
    top = (104 if standalone else 52) + 30
    H = top + rows_n * (panel_h + 40) + (18 * len(footer_lines) + 40 if standalone else 10)
    svg = Svg(W, H, f'{title}. {subtitle}')
    header(svg, meta, title, subtitle, True)
    cursor = version_legend(svg, pad, top - 12, meta['versions'])
    svg.line(cursor, top - 16, cursor + 22, top - 16, THEME['muted'], 1.5)
    svg.text(cursor + 30, top - 10.5, 'Ideal linear scaling (N ×)', 13, THEME['text2'])
    for index, panel in enumerate(panels):
        px = pad + (index % cols) * (panel_w + 40)
        py = top + 20 + (index // cols) * (panel_h + 40)
        svg.text(px, py + 4, panel['title'], 14.5, THEME['text'], weight=600)
        gx0, gx1 = px + 46, px + panel_w - 120
        gy0, gy1 = py + 22, py + panel_h - 40
        counts = [c['threads'] for c in panel['configs']]
        most = max(counts)
        ymax = max(float(most), max(p['scaling'][v]['scaling'] for p in panel['configs'] for v in ('before', 'after')) * 1.08)
        ymax, ystep = nice_max(ymax, 4)
        sx = lambda n: gx0 + (gx1 - gx0) * n / most
        sy = lambda value: gy1 - (gy1 - gy0) * value / ymax
        value = 0.0
        while value <= ymax + 1e-9:
            svg.line(gx0, sy(value), gx1, sy(value), THEME['grid'])
            svg.text(gx0 - 8, sy(value) + 4, f'{value:g}×', 11.5, THEME['muted'], 'end', mono=True)
            value += ystep
        for n in counts:
            svg.text(sx(n), gy1 + 18, f'{n}', 12, THEME['muted'], 'middle', mono=True)
        svg.text((gx0 + gx1) / 2, gy1 + 36, 'Worker threads', 12, THEME['text2'], 'middle')
        svg.line(gx0, gy1, gx1, gy1, THEME['axis'])
        svg.line(sx(1), sy(1), sx(most), sy(most), THEME['muted'], 1.5, ' stroke-opacity="0.7"')
        ends = []
        for version in ('before', 'after'):
            pts = [(sx(c['threads']), sy(c['scaling'][version]['scaling']), c) for c in panel['configs']]
            path = ' '.join(f'{"M" if i == 0 else "L"}{x:.1f},{y:.1f}' for i, (x, y, c) in enumerate(pts))
            svg.add(f'<path d="{path}" fill="none" stroke="{THEME[version]}" stroke-width="2" '
                    f'stroke-linejoin="round" stroke-linecap="round"/>')
            for x, y, c in pts:
                s = c['scaling'][version]
                rows = [[f'{VERSION_LABEL[version]} · {c["threads"]} threads', f'{s["scaling"]:.2f}× vs 1 thread'],
                        ['Parallel efficiency', f'{s["efficiency_percent"]:.0f}%'],
                        ['Throughput (median)', f'{fmt_mlps(c[version]["throughput_mlps"]["median"])} M/s']]
                svg.add(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="5" fill="{THEME[version]}" stroke="{THEME["surface"]}" '
                        f'stroke-width="2" class="mark" data-threads="{c["threads"]}"{tip(rows)}/>')
            last = panel['configs'][-1]['scaling'][version]
            ends.append([pts[-1][1], version, f'{last["scaling"]:.2f}× · {last["efficiency_percent"]:.0f}%'])
        ends.sort()
        if ends[1][0] - ends[0][0] < 18:
            shift = (18 - (ends[1][0] - ends[0][0])) / 2
            ends[0][0] -= shift
            ends[1][0] += shift
        for y, version, label in ends:
            svg.rect(gx1 + 12, y - 5, 10, 10, THEME[version], rx=2)
            svg.text(gx1 + 28, y + 4.5, label, 12, THEME['text'], mono=True)
    if standalone:
        footer(svg, meta, footer_lines)
    return svg.render(standalone=standalone)


def grouped_bars(meta, title, subtitle, groups, unit, axis_title, footer_lines, higher=False,
                 fmt=None, note=None):
    """Generic grouped horizontal bars: groups of (label, before, after, extra rows)."""
    fmt = fmt or (lambda v: f'{v:,.1f} {unit}')
    footer_lines = wrap_footer(footer_lines, 1100 - 64)
    standalone = meta['standalone']
    W, pad = 1100, 32
    label_w, right_w = 290, 200
    x0 = pad + label_w + 30
    x1 = W - pad - right_w
    bar_h, inner, gap = 20, 2, 22
    plot_h = len(groups) * (2 * bar_h + inner) + (len(groups) - 1) * gap
    top = (104 if standalone else 52) + 34
    axis_y = top + plot_h + 12
    H = axis_y + 62 + (24 if note else 0) + (18 * len(footer_lines) + 30 if standalone else 0)
    svg = Svg(W, H, f'{title}. {subtitle}')
    header(svg, meta, title, subtitle, higher)
    version_legend(svg, pad, top - 22, meta['versions'])
    svg.text(x1 + 24, top - 20.5, 'Change (After vs Before)', 12, THEME['muted'])
    maximum, step = nice_max(max(max(g['before'], g['after']) for g in groups) * 1.18 or 1)
    scale = lambda value: x0 + (x1 - x0) * value / maximum
    value = 0.0
    while value <= maximum + 1e-9:
        svg.line(scale(value), top - 6, scale(value), axis_y, THEME['grid'])
        value += step
    y = top
    for g in groups:
        mid = y + bar_h + inner / 2
        lines = g['label'] if isinstance(g['label'], list) else [g['label']]
        svg.text(pad, mid - 4 - 8 * (len(lines) - 1), lines[0], 14, THEME['text'], weight=600)
        for k, extra_line in enumerate(lines[1:]):
            svg.text(pad, mid + 13 + 15 * k - 8 * (len(lines) - 2), extra_line, 11.5, THEME['muted'])
        for index, version in enumerate(('before', 'after')):
            by = y + index * (bar_h + inner)
            value = g[version]
            rows = [[f'{VERSION_LABEL[version]} · {" ".join(lines)}', fmt(value)]] + g.get('tip', {}).get(version, [])
            svg.text(x0 - 10, by + bar_h / 2 + 4.5, VERSION_LABEL[version], 12, THEME['muted'], 'end')
            svg.bar(x0, by, scale(value), bar_h, THEME[version],
                    extra=f' class="mark"{tip(rows)}')
            svg.text(scale(value) + 8, by + bar_h / 2 + 4.5, fmt(value), 12.5, THEME['text'], mono=True)
        change = g.get('change_label')
        if change:
            svg.text(x1 + 24, mid + 5, change, 13.5, THEME['text'], weight=700)
        y += 2 * bar_h + inner + gap
    x_axis(svg, x0, x1, axis_y, maximum, step, scale, axis_title)
    if note:
        svg.text(pad, axis_y + 70, note, 12, THEME['text2'])
    if standalone:
        footer(svg, meta, footer_lines)
    return svg.render(standalone=standalone)


def speedup_matrix(meta, title, subtitle, rows, columns, footer_lines):
    """Heat table of throughput speedups; every cell is labeled."""
    footer_lines = wrap_footer(footer_lines, 1100 - 64)
    standalone = meta['standalone']
    W, pad = 1100, 32
    label_w = 360
    cell_w = (W - 2 * pad - label_w) / len(columns)
    cell_h = 30
    top = (104 if standalone else 52) + 88
    H = top + cell_h * len(rows) + 120 + (18 * len(footer_lines) + 30 if standalone else 0)
    svg = Svg(W, H, f'{title}. {subtitle}')
    header(svg, meta, title, subtitle, True)
    # Diverging encoding around 1x: regressions toward red, gains toward mint.
    def color(s):
        if s is None:
            return THEME['surface2'], THEME['muted']
        v = max(-1.0, min(1.0, math.log2(s) / 2))
        if v >= 0:
            a = 0.18 + 0.82 * v
            return f'rgba(85,241,177,{a:.3f})', ('#08110d' if a > 0.55 else THEME['text'])
        a = 0.25 + 0.75 * (-v)
        return f'rgba(230,103,103,{a:.3f})', ('#1a0707' if a > 0.6 else THEME['text'])
    # Version identities of the ratio shown in every cell.
    svg.text(pad, top - 58, f'Each cell: median throughput of After · {meta["versions"]["after"]} divided by '
             f'Before · {meta["versions"]["before"]}', 13, THEME['text2'])
    for j, col in enumerate(columns):
        cx = pad + label_w + j * cell_w + cell_w / 2
        svg.text(cx, top - 30, col['top'], 12, THEME['text2'], 'middle', 600)
        svg.text(cx, top - 12, col['bottom'], 12, THEME['muted'], 'middle')
    y = top
    for row in rows:
        svg.text(pad, y + cell_h / 2 + 4.5, row['label'], 12.5, THEME['text'])
        for j, cell in enumerate(row['cells']):
            x = pad + label_w + j * cell_w
            fill, ink = color(cell and cell['speedup'])
            rows_tip = [[row['label'] + ' · ' + columns[j]['top'] + ' ' + columns[j]['bottom'],
                         fmt_speedup(cell['speedup']) if cell else 'n/a']]
            if cell:
                rows_tip += [['Change', fmt_pct(cell['change'])],
                             ['95% CI', f'×{cell["low"]:.2f} – ×{cell["high"]:.2f}'],
                             ['Before → After', f'{fmt_mlps(cell["before"])} → {fmt_mlps(cell["after"])} M/s']]
            svg.add(f'<g class="mark" data-threads="{columns[j]["threads"]}"{tip(rows_tip)}>')
            svg.rect(x + 1, y + 1, cell_w - 2, cell_h - 2, fill, rx=3)
            if cell:
                mark = '' if cell['direction'] == 'faster' else (' ▼' if cell['direction'] == 'slower' else ' ≈')
                svg.text(x + cell_w / 2, y + cell_h / 2 + 4.5, f'{cell["speedup"]:.2f}×{mark}', 12.5, ink, 'middle', 600, mono=True)
            svg.add('</g>')
        y += cell_h
    ly = y + 30
    svg.text(pad, ly, 'Cell = median throughput speedup (After / Before); color: mint = faster, red = slower. '
             'Unmarked cells are conclusive gains (95% CI above 1×).', 12, THEME['text2'])
    svg.text(pad, ly + 18, '▼ marks a conclusive regression (95% CI below 1×); ≈ marks an inconclusive difference '
             '(95% CI spans 1×).', 12, THEME['text2'])
    sx = pad
    svg.text(sx, ly + 50, 'Color scale', 12, THEME['muted'])
    sx += 82
    for value in (0.5, 0.8, 1.0, 1.5, 2.0, 4.0):
        fill, ink = color(value)
        svg.rect(sx, ly + 36, 54, 20, fill, rx=3)
        svg.text(sx + 27, ly + 50.5, f'{value:g}×', 11.5, ink, 'middle', 600, mono=True)
        sx += 58
    if standalone:
        footer(svg, meta, footer_lines)
    return svg.render(standalone=standalone)
