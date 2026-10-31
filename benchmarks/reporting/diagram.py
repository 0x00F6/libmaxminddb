"""Diagram of the automatic radix-16 preparation (fd08b22) and of a prepared
lookup, with the numbers of the campaign: tree size, memory, opening time and
lookup speed. Drawn with the chart helpers so it matches the other figures."""
from charts import THEME, Svg, fmt_ms, fmt_speedup, wrap_footer

W = 1100
WARN = '#e8b44c'
WARN_BG = '#3a2a12'


def box(svg, x, y, w, h, stroke, fill=None, rx=10, dash=''):
    extra = f' stroke="{stroke}" stroke-width="1.3"' + (f' stroke-dasharray="{dash}"' if dash else '')
    svg.rect(x, y, w, h, fill or THEME['page'], rx, extra)


def lines(svg, x, y, items, size=13, gap=19, color=None):
    """Text lines; items wrapped in backticks are code."""
    for i, item in enumerate(items):
        code = item.startswith('`')
        svg.text(x, y + i * gap, item.strip('`'), size - (0.5 if code else 0), color or THEME['text2'], mono=code)


def arrow(svg, x1, y, x2, above, below):
    svg.line(x1, y, x2 - 8, y, THEME['muted'], 2)
    svg.add(f'<path d="M{x2 - 9},{y - 5} L{x2},{y} L{x2 - 9},{y + 5} z" fill="{THEME["muted"]}"/>')
    svg.text((x1 + x2) / 2, y - 9, above, 11.5, THEME['muted'], 'middle')
    svg.text((x1 + x2) / 2, y + 19, below, 11.5, THEME['muted'], 'middle')


def section(svg, y, label):
    svg.text(32, y, label, 16, THEME['accent'], weight=700)


def facts(ctx, size):
    """The campaign's numbers shown in the diagram."""
    city = ctx.databases['city']
    native = city['node_count'] * 8 / 2 ** 20
    tables = 1.0 if city['ip_version'] == 6 else 0.5
    memory = next(m for m in ctx.s['memory'] if m['database'] == 'city' and m['family'] == 4
                  and m['queries'] == size and m['threads'] == 1)
    start = {r['version']: r['open_ms']['median'] for r in ctx.s['startup']
             if r['database'] == 'city' and not r['cold'] and r['variant'] == 'default'}
    lookup = ctx.one('throughput', database='city', profile='random', operation='lookup', family=4,
                     queries=size, threads=1)
    attribution = [a for a in ctx.s.get('attribution') or [] if a['database'] == 'city']
    span = lambda key: (f'{fmt_speedup(min(a[key]["ratio"] for a in attribution))}–'
                        f'{fmt_speedup(max(a[key]["ratio"] for a in attribution))}') if attribution else None
    cycles = {v: (lookup[v].get('core_cycles_per_lookup') or {}).get('median') for v in ('before', 'after')}
    return {'city': city, 'native': native, 'tables': tables, 'heap': memory['comparison']['heap_after_open_mib']['delta'],
            'open': start, 'ns': {v: 1000 / lookup[v]['throughput_mlps']['median'] for v in ('before', 'after')},
            'speedup': lookup['comparison']['speedup'], 'cycles': cycles,
            'packed': span('packed_vs_before'), 'prepared': span('after_vs_packed')}


def radix16_diagram(ctx, size, title, footer_lines):
    f = facts(ctx, size)
    city = f['city']
    footer_lines = wrap_footer(footer_lines, W - 64)
    svg = Svg(W, 1430 + 18 * len(footer_lines), f'{title}: built once by MMDB_open, then every lookup skips up to 16 tree levels with one load')
    svg.text(32, 46, title, 24, THEME['text'], weight=700)
    svg.text(32, 72, 'Built once by MMDB_open(): a native child array and 16-bit root tables. Every lookup then skips '
             'up to 16 tree levels with one load.', 13.5, THEME['muted'])

    # 1. Preparation.
    section(svg, 114, '① MMDB_open() prepares the index once (not with MMDB_NO_PREPARE)')
    box(svg, 32, 130, 290, 280, THEME['line'], THEME['surface2'])
    svg.text(48, 158, 'Mapped .mmdb file (read-only)', 14.5, THEME['text'], weight=700)
    box(svg, 48, 172, 258, 132, THEME['before'], THEME['page'], 8)
    svg.text(62, 197, 'Packed search tree', 13.5, THEME['text'], weight=650)
    lines(svg, 62, 220, ['N nodes of 6, 7 or 8 bytes', '24, 28 or 32-bit records,',
                         'unaligned: every step decodes', 'a packed record'])
    box(svg, 48, 312, 258, 42, THEME['line'], THEME['page'], 8)
    svg.text(62, 338, 'Data section (unchanged)', 13, THEME['text2'])
    box(svg, 48, 362, 258, 36, THEME['line'], THEME['page'], 8)
    svg.text(62, 385, 'Metadata', 13, THEME['text2'])
    arrow(svg, 324, 240, 366, 'decode', 'once')

    box(svg, 368, 130, 330, 280, THEME['after'], THEME['surface2'])
    svg.text(384, 158, 'Native array nodes[N][2]', 14.5, THEME['text'], weight=700)
    lines(svg, 384, 182, ['uint32_t left + right: 8 bytes per node', '64-byte aligned: 8 nodes per cache',
                          'line, no node straddles two lines'])
    cell = 37
    svg.text(384 + 4 * cell, 254, '← one 64-byte cache line →', 11.5, THEME['muted'], 'middle')
    for i in range(8):
        x = 384 + i * cell
        box(svg, x, 262, cell - 3, 34, THEME['after'], THEME['page'], 4)
        svg.line(x + (cell - 3) / 2, 264, x + (cell - 3) / 2, 294, THEME['line'])
        svg.text(x + (cell - 3) / 4, 284, 'L', 10.5, THEME['muted'], 'middle', mono=True)
        svg.text(x + 3 * (cell - 3) / 4, 284, 'R', 10.5, THEME['muted'], 'middle', mono=True)
        svg.text(x + (cell - 3) / 2, 312, f'n{i}', 10.5, THEME['muted'], 'middle', mono=True)
    lines(svg, 384, 340, [f'GeoLite2-City {city["build_date"]}:', f'{city["node_count"]:,} nodes → {f["native"]:.1f} MiB'])
    svg.text(384, 386, 'Over 64 MiB: not prepared (packed fallback)', 13, WARN)
    arrow(svg, 700, 240, 742, 'first 16', 'levels')

    box(svg, 744, 130, 324, 280, THEME['accent'], THEME['surface2'])
    svg.text(760, 158, 'Root table root[65,536]', 14.5, THEME['text'], weight=700)
    svg.text(760, 182, '8 bytes per entry = 512 KiB, 64-byte aligned', 13, THEME['text2'])
    x = 760
    for width, bits, label, color in ((26, '63', 'E', WARN), (114, '62…40', 'reserved', THEME['line']),
                                      (68, '39…32', 'depth', THEME['accent']), (84, '31…0', 'node', THEME['after'])):
        box(svg, x, 196, width - 2, 32, color, THEME['page'], 3)
        svg.text(x + (width - 2) / 2, 217, label, 11.5, THEME['muted'] if color == THEME['line'] else THEME['text'],
                 'middle', 600)
        svg.text(x + (width - 2) / 2, 243, bits, 10, THEME['muted'], 'middle', mono=True)
        x += width
    lines(svg, 760, 266, ['E: corrupt branch, reported only', 'when a lookup selects the entry'], 12, 16, THEME['muted'])
    box(svg, 760, 304, 292, 92, THEME['accent'], THEME['page'], 8, '5 4')
    svg.text(774, 328, 'ipv4_root[65,536] (IPv6 databases)', 13.5, THEME['text'], weight=650)
    lines(svg, 774, 350, ['same, from ipv4_start_node, the node', 'reached after ::/96 — 512 KiB'])

    # 2. Filling.
    section(svg, 450, '② Filling: a leaf above depth 16 covers all of its keys, with its exact depth')
    nodes = {'r': (165, 486), 'a': (100, 534), 'b': (230, 534), 'aa': (65, 582), 'ab': (135, 582),
             'ba': (195, 582), 'bb': (265, 582)}
    for parent, child in (('r', 'a'), ('r', 'b'), ('a', 'aa'), ('a', 'ab'), ('b', 'ba'), ('b', 'bb')):
        (x1, y1), (x2, y2) = nodes[parent], nodes[child]
        svg.line(x1, y1, x2, y2, THEME['muted'], 1.5)
    for name, (x, y) in nodes.items():
        if name == 'aa':
            box(svg, x - 22, y - 14, 44, 28, WARN, WARN_BG, 6)
            svg.text(x, y + 5, 'T', 13, THEME['text'], 'middle', 700)
        else:
            svg.add(f'<circle cx="{x}" cy="{y}" r="12" fill="{THEME["page"]}" stroke="{THEME["after"]}" stroke-width="1.5"/>')
    for name in ('ab', 'ba', 'bb'):
        x, y = nodes[name]
        svg.line(x, y + 12, x, y + 32, THEME['muted'], 1.5, ' stroke-dasharray="3 3"')
    svg.text(65, 612, 'T: data or empty', 11.5, WARN, 'middle')
    svg.text(32, 640, '… down to depth 16', 11.5, THEME['muted'])
    for y, depth in ((490, 0), (538, 1), (586, 2)):
        svg.text(300, y, f'depth {depth}', 11, THEME['muted'], mono=True)
    bx, bw, by = 392, 676, 498
    svg.text(bx, 484, 'root[key], key = first 16 address bits', 13, THEME['text2'])
    box(svg, bx, by, bw / 4, 42, WARN, WARN_BG, 4)
    svg.text(bx + bw / 8, by + 18, 'T, depth 2', 12.5, THEME['text'], 'middle', 650)
    svg.text(bx + bw / 8, by + 34, '16,384 identical entries', 11, THEME['text2'], 'middle')
    step = (3 * bw / 4 - 4) / 36
    for i in range(36):
        box(svg, bx + bw / 4 + 4 + i * step, by, step - 2, 42, THEME['after'], THEME['page'], 1)
    for x, label, anchor in ((bx, '0x0000', 'start'), (bx + bw / 4, '0x4000', 'middle'), (bx + bw, '0xFFFF', 'end')):
        svg.text(x, by + 58, label, 10.5, THEME['muted'], anchor, mono=True)
    svg.text(bx + 5 * bw / 8, by + 58, 'elsewhere: the node reached at depth 16', 11.5, THEME['muted'], 'middle')
    lines(svg, bx, 604, ['A leaf at depth d fills 2^(16 − d) consecutive entries and keeps d, so prefix',
                         'lengths and not-found results are exactly those of the bit-by-bit walk.'])

    # 3. Lookup.
    section(svg, 680, '③ Every lookup: MMDB_lookup_sockaddr() and MMDB_lookup_string()')
    steps = [
        ('1. Start', THEME['before'], ['IPv6: node 0, bit 0, root', 'IPv4 in an IPv6 database:', 'ipv4_start_node, bit 96,',
                                       'ipv4_root']),
        ('2. One table load', THEME['accent'], ['key = next 16 address bits', '`entry = root[key]`',
                                                'node, and bit += depth', 'skips up to 16 levels']),
        ('3. Native steps', THEME['after'], ['while bit < depth, node < N:', '`node = nodes[node][bit]`',
                                             'one indexed load per level']),
        ('4. Result', THEME['text2'], ['node ≥ N: data (found)', 'or empty (not found)', 'netmask = bit',
                                       'same result as Before']),
    ]
    sw, gap = 241, 21
    for i, (name, color, items) in enumerate(steps):
        x = 32 + i * (sw + gap)
        box(svg, x, 696, sw, 132, color, THEME['surface2'])
        svg.text(x + 14, 722, name, 14, THEME['text'], weight=700)
        lines(svg, x + 14, 746, items, 12.5, 18)
        if i:
            svg.line(x - gap + 3, 762, x - 9, 762, THEME['muted'], 2)
            svg.add(f'<path d="M{x - 10},{757} L{x - 2},{762} L{x - 10},{767} z" fill="{THEME["muted"]}"/>')
    box(svg, 32, 842, 1036, 58, THEME['line'], THEME['surface2'], 8)
    svg.text(48, 866, 'Example: 1.2.3.4 in GeoLite2-City (IPv6 database, tree depth 128)', 13.5, THEME['text'], weight=650)
    svg.text(48, 888, 'ipv4_root[0x0102] skips bits 96–111 in one load, then at most 16 native steps (bits 112–127) '
             'instead of 32 packed steps.', 12.5, THEME['text2'])

    # 4. Why it is faster.
    section(svg, 942, '④ Why it is faster')
    cycles = f['cycles']
    measured = [f'{f["ns"]["before"]:.0f} ns → {f["ns"]["after"]:.0f} ns per random IPv4 lookup on one thread '
                f'({fmt_speedup(f["speedup"])})']
    if cycles['before'] and cycles['after']:
        measured.append(f'{cycles["before"]:.0f} → {cycles["after"]:.0f} core cycles per lookup (APERF)')
    if f['packed'] and f['prepared']:
        measured += [f'packed traversal alone {f["packed"]}; preparation', f'on top of it {f["prepared"]} (separate runs)']
    measured.append(f'on {ctx.cpu}')
    reasons = [
        ('Fewer dependent steps', THEME['accent'], [
            'A lookup is a chain of dependent loads: each step needs', 'the node read by the previous one. One table load',
            'replaces the first 16 levels, which every lookup crosses;', 'leaves above depth 16, such as most random IPv6',
            'misses, end with that single load.']),
        ('Cheaper steps', THEME['after'], [
            'Native nodes need no 24/28/32-bit unpacking, no', 'indirect record-getter call and no branch on the',
            'address bit: the child is nodes[node][bit], one indexed', 'load. Random address bits feed an index',
            'instead of a branch to predict.']),
        ('One cache line per step', THEME['before'], [
            '8-byte nodes in a 64-byte-aligned array never straddle', 'two cache lines. Packed 7-byte nodes (28-bit records)',
            'sometimes do, and then a step waits for two lines.', 'A prepared lookup reads one 8-byte root entry, then',
            'one 8-byte node per remaining level.']),
        ('Measured in this campaign', THEME['text2'], measured),
    ]
    for i, (name, color, items) in enumerate(reasons):
        x, y = 32 + (i % 2) * 528, 958 + (i // 2) * 162
        box(svg, x, y, 508, 148, color, THEME['surface2'])
        svg.text(x + 16, y + 26, name, 14, THEME['text'], weight=700)
        lines(svg, x + 16, y + 50, items, 12.5, 18)

    # 5. Cost.
    total = f['native'] + f['tables']
    tables = '2 × 512 KiB' if f['tables'] == 1.0 else '512 KiB'
    section(svg, 1310, '⑤ Cost and compatibility')
    lines(svg, 32, 1336, [
        f'Memory: {f["native"]:.1f} MiB of nodes + {tables} of tables ≈ {total:.1f} MiB of heap per open reader '
        f'(measured: +{f["heap"]:.1f} MiB).',
        f'MMDB_open on GeoLite2-City: {fmt_ms(f["open"]["before"])} → {fmt_ms(f["open"]["after"])} with a warm page '
        'cache, paid once per open.',
        'Fallback: MMDB_NO_PREPARE, a failed allocation or more than 64 MiB of nodes keep the specialized packed traversal.',
        'MMDB_s and SONAME 0 are unchanged: the prepared state lives in the private allocation of the reader\'s filename.'],
        12.5, 18)
    y = svg.height - 18 - 18 * (len(footer_lines) - 1)
    svg.line(32, y - 22, W - 32, y - 22, THEME['line'])
    for line in footer_lines:
        svg.text(32, y, line, 12, THEME['muted'])
        y += 18
    return svg.render()
