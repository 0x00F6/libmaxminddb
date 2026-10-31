#!/usr/bin/env python3
"""Render every exported SVG chart to a 2x PNG with headless Chrome.

The PNG files are pixel renders of the same SVG files written by report.py, so
images, the HTML report and the pull request draft share one data source. The
image manifest is updated with the PNG hashes and verified pixel sizes.
"""
import argparse
import json
import re
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import report  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
SCALE = 2


def png_size(path):
    header = path.read_bytes()[:24]
    if header[:8] != b'\x89PNG\r\n\x1a\n':
        raise ValueError(f'{path} is not a PNG file')
    return struct.unpack('>II', header[16:24])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', default=str(ROOT / 'benchmarks/reports'))
    parser.add_argument('--chrome', default=shutil.which('google-chrome') or shutil.which('chromium'))
    args = parser.parse_args()
    report.set_output(args.output)
    assets = report.ASSETS
    if not args.chrome:
        raise SystemExit('Chrome or Chromium is required to render PNG files')
    manifest_path = assets / 'manifest.json'
    manifest = json.loads(manifest_path.read_text())
    with tempfile.TemporaryDirectory(prefix='render-') as temp:
        temp = Path(temp)
        for item in manifest:
            svg_path = report.REPORTS / item['svg']
            svg = svg_path.read_text()
            width = int(re.search(r'<svg[^>]* width="(\d+)"', svg).group(1))
            height = int(re.search(r'<svg[^>]* height="(\d+)"', svg).group(1))
            page = temp / f'{item["id"]}.html'
            page.write_text('<!DOCTYPE html><html><head><meta charset="utf-8"><style>'
                            'html,body{margin:0;padding:0;background:#10191f;overflow:hidden}svg{display:block}'
                            '</style></head><body>' + svg.split('?>', 1)[-1] + '</body></html>')
            png_path = report.REPORTS / item['png']
            command = [args.chrome, '--headless=new', '--disable-gpu', '--no-first-run',
                       '--no-default-browser-check', '--hide-scrollbars', f'--user-data-dir={temp / "profile"}',
                       f'--force-device-scale-factor={SCALE}', f'--window-size={width},{height}',
                       f'--screenshot={png_path}', page.as_uri()]
            subprocess.run(command, check=True, capture_output=True, timeout=120)
            actual = png_size(png_path)
            if actual != (SCALE * width, SCALE * height):
                raise SystemExit(f'{png_path}: {actual} instead of {(SCALE * width, SCALE * height)}')
            item['png_size'] = list(actual)
    report.write_manifest(manifest)
    print(f'{len(manifest)} PNG files rendered at {SCALE}x')


if __name__ == '__main__':
    main()
