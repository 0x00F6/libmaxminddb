// Exercise the interactive HTML report in headless Chrome through the DevTools
// protocol: filters, thread highlighting, tooltips, keyboard focus,
// table sorting and console errors. Screenshots are written for visual review.
// Usage: node check_html.mjs REPORT.html OUTPUT_DIRECTORY [SUMMARY.json]
// SUMMARY.json defaults to performance-comparison-data/summary.json next to the
// report; for a merged report, give the summary of its first architecture.
import { spawn } from 'node:child_process';
import { mkdirSync, readFileSync, writeFileSync, mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

const [report, outDir, summaryPath] = process.argv.slice(2);
if (!report || !outDir) {
  console.error('Usage: node check_html.mjs REPORT.html OUTPUT_DIRECTORY [SUMMARY.json]');
  process.exit(2);
}
mkdirSync(outDir, { recursive: true });
// The expected CPU, workload size and worker counts come from the data the report was generated from.
const summary = JSON.parse(readFileSync(summaryPath ||
  join(dirname(resolve(report)), 'performance-comparison-data', 'summary.json'), 'utf8'));
const cpu = summary.environment.cpu.model;
const queries = summary.throughput[0].queries;
const size = queries % 1000000 === 0 ? `${queries / 1000000}M` : `${queries / 1000}K`;
const threads = [...new Set(summary.throughput.map((c) => c.threads))].sort((a, b) => a - b);
const highlight = String(threads[Math.min(1, threads.length - 1)]);
const port = 9300 + Math.floor(Math.random() * 500);
const profile = mkdtempSync(join(tmpdir(), 'report-check-'));
const chrome = spawn(process.env.CHROME || 'google-chrome', [
  '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
  `--remote-debugging-port=${port}`, `--user-data-dir=${profile}`, '--window-size=1400,1000',
  '--hide-scrollbars', 'about:blank'], { stdio: 'ignore' });

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
async function endpoint() {
  for (let i = 0; i < 100; i++) {
    try {
      const pages = await (await fetch(`http://127.0.0.1:${port}/json`)).json();
      const page = pages.find((p) => p.type === 'page');
      if (page) return page.webSocketDebuggerUrl;
    } catch { /* not ready */ }
    await sleep(100);
  }
  throw new Error('Chrome DevTools endpoint unavailable');
}

const ws = new WebSocket(await endpoint());
await new Promise((r) => ws.addEventListener('open', r, { once: true }));
let next = 0;
const pending = new Map();
const events = [];
ws.addEventListener('message', (message) => {
  const data = JSON.parse(message.data);
  if (data.id && pending.has(data.id)) {
    pending.get(data.id)(data);
    pending.delete(data.id);
  } else if (data.method) {
    events.push(data);
  }
});
const send = (method, params = {}) => new Promise((resolve_) => {
  const id = ++next;
  pending.set(id, resolve_);
  ws.send(JSON.stringify({ id, method, params }));
});
async function evaluate(expression) {
  const result = await send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true });
  if (result.result.exceptionDetails) throw new Error(JSON.stringify(result.result.exceptionDetails));
  return result.result.result.value;
}
const results = [];
function check(name, ok, detail = '') {
  results.push({ name, ok: Boolean(ok), detail });
  console.log(`${ok ? 'PASS' : 'FAIL'} ${name}${detail ? ' — ' + detail : ''}`);
}
async function screenshot(name, selector) {
  if (selector) {
    await evaluate(`document.querySelector(${JSON.stringify(selector)}).scrollIntoView({block:'start', behavior:'instant'})`);
    await sleep(150);
  }
  const shot = await send('Page.captureScreenshot', { format: 'png' });
  writeFileSync(join(outDir, `${name}.png`), Buffer.from(shot.result.data, 'base64'));
}

await send('Page.enable');
await send('Runtime.enable');
await send('Log.enable');
await send('Emulation.setDeviceMetricsOverride', { width: 1400, height: 1000, deviceScaleFactor: 1, mobile: false });
await send('Page.navigate', { url: pathToFileURL(resolve(report)).href });
for (let i = 0; i < 100 && !events.some((e) => e.method === 'Page.loadEventFired'); i++) await sleep(100);
await sleep(300);

const visible = `[...document.querySelectorAll('[data-panel]')].filter(p => !p.hidden && p.querySelector('h3')).map(p => p.querySelector('h3').textContent)`;
const initial = await evaluate(visible);
check('initial panels visible', initial.length > 0, initial.join(' | '));
check(`${size} throughput panels lead`, initial[0]?.includes('Throughput') && initial[0]?.includes(`(${size})`), initial[0]);
check(`required IPv4 ${size} title`, initial.includes(`IPv4 Throughput — Random Lookups (${size})`));
check(`required IPv6 ${size} title`, initial.includes(`IPv6 Throughput — Random Lookups (${size})`));
check('no 1M panels', !initial.some((t) => t.includes('(1M)')), initial.filter((t) => t.includes('(1M)')).join(' | '));
check('candlestick IPv4 visible', initial.some((t) => t.startsWith('Candlestick Percentile Rank — IPv4 Lookups')));
check('candlestick IPv6 visible', initial.some((t) => t.startsWith('Candlestick Percentile Rank — IPv6 Lookups')));
const title = await evaluate('document.title');
check('CPU model in <title>', title.includes(cpu), title);
const h1 = await evaluate(`document.querySelector('h1').textContent`);
check('CPU model in main header', h1.includes(cpu), h1);
await screenshot('01-top');

// Merged report: the Architecture filter shows one architecture at a time.
const arches = await evaluate(`[...document.querySelectorAll('[data-control="arch"] button')].map(b => b.dataset.value)`);
if (arches.length > 1) {
  const shown = `[...new Set([...document.querySelectorAll('[data-arch]')].filter(e => !e.hidden).map(e => e.dataset.arch))]`;
  check('one architecture shown at first', JSON.stringify(await evaluate(shown)) === JSON.stringify([arches[0]]));
  const heading = `[...document.querySelectorAll('[data-arch]')].filter(e => !e.hidden).map(e => e.querySelector('h1')).find(Boolean).textContent`;
  for (const value of arches.slice(1)) {
    await evaluate(`document.querySelector('[data-control="arch"] button[data-value="${value}"]').click()`);
    const visibleNow = await evaluate(shown);
    const panels = await evaluate(visible);
    const offered = await evaluate(`[...document.querySelectorAll('[data-arches]')].filter(b => !b.hidden).every(b => b.dataset.arches.split(' ').includes('${value}'))`);
    check(`architecture switch to ${value}`, JSON.stringify(visibleNow) === JSON.stringify([value]) && panels.length > 0 && offered
      && (await evaluate(heading)) !== h1, JSON.stringify(visibleNow));
    check(`architecture ${value} kept in the URL`, (await evaluate('location.search')).includes(`arch=${value}`));
    await screenshot(`01-top-${value}`);
  }
  await evaluate(`document.querySelector('[data-control="arch"] button[data-value="${arches[0]}"]').click()`);
  check('architecture switch back', JSON.stringify(await evaluate(shown)) === JSON.stringify([arches[0]]));
}
await screenshot('02-throughput', '#throughput');

const click = (control, value) => evaluate(
  `document.querySelector('[data-control="${control}"] button[data-value="${value}"]').click()`);
// Filters exist only for the values the campaign measured.
const has = (control, value) => evaluate(
  `!!document.querySelector('[data-control="${control}"] button[data-value="${value}"]')`);
let now;
if (await has('db', 'synthetic')) {
  await click('db', 'synthetic');
  now = await evaluate(visible);
  check('database filter switches panels', now.length === initial.length &&
    now.every((t, i) => t === initial[i] || (t.includes('Matrix') && t.includes('Synthetic'))),
    'same chart set, synthetic data and matrix');
  const subtitle = await evaluate(`[...document.querySelectorAll('[data-panel]')].find(p => !p.hidden && p.querySelector('.sub')).querySelector('.sub').textContent`);
  check('database filter shows synthetic subtitle', subtitle.includes('Synthetic'), subtitle);
  await click('db', 'city');
}
if (await has('profile', 'hits')) {
  await click('profile', 'hits');
  now = await evaluate(visible);
  check('profile filter switches titles', now.some((t) => t.includes('Populated-Network')), now[0]);
}
if (await has('op', 'decode')) {
  await click('op', 'decode');
  now = await evaluate(visible);
  check('operation filter switches titles', now.some((t) => t.includes('Field Decode')), now[0]);
}
await screenshot('03-filtered', '#throughput');
if (await has('profile', 'random')) await click('profile', 'random');
if (await has('op', 'lookup')) await click('op', 'lookup');

await click('threads', highlight);
const dims = await evaluate(`({dim: document.querySelectorAll('.dim').length, groups: document.querySelectorAll('[data-panel]:not([hidden]) g.group').length})`);
check('thread highlight dims other groups', threads.length < 2 || dims.dim > 0, JSON.stringify(dims));
await screenshot(`04-threads-${highlight}`, '#throughput');
await click('threads', 'all');
check('thread highlight resets', (await evaluate(`document.querySelectorAll('[data-panel]:not([hidden]) .dim').length`)) === 0);

const candles = await evaluate(`[...document.querySelectorAll('[data-panel][data-chart="candlestick"]')].filter(p => !p.hidden).map(p => p.querySelector('h3').textContent)`);
check(`candlesticks show ${size} queries`, candles.length > 0 && candles.every((t) => t.includes(`(${size})`)), candles.join(' | '));
await screenshot('05-candlestick', '#latency');

await evaluate(`[...document.querySelectorAll('[data-panel]:not([hidden]) .mark[data-tip]')][0].scrollIntoView({block:'center', behavior:'instant'})`);
await sleep(300);
const box = await evaluate(`(() => { const el = [...document.querySelectorAll('[data-panel]:not([hidden]) .mark[data-tip]')][0];
  const r = el.getBoundingClientRect(); return {x: r.left + r.width / 2, y: r.top + r.height / 2}; })()`);
await send('Input.dispatchMouseEvent', { type: 'mouseMoved', x: box.x, y: box.y });
await sleep(150);
const tip = await evaluate(`(() => { const t = document.getElementById('tooltip'); return {display: getComputedStyle(t).display, text: t.textContent}; })()`);
check('hover shows tooltip with exact value', tip.display === 'block' && /M lookups\/s/.test(tip.text), tip.text.slice(0, 120));
await screenshot('06-tooltip');
await send('Input.dispatchMouseEvent', { type: 'mouseMoved', x: 5, y: 5 });
await sleep(100);
check('tooltip hides when leaving marks', (await evaluate(`getComputedStyle(document.getElementById('tooltip')).display`)) === 'none');
await evaluate(`[...document.querySelectorAll('[data-panel]:not([hidden]) .mark[data-tip]')][3].focus()`);
await sleep(100);
const focusTip = await evaluate(`getComputedStyle(document.getElementById('tooltip')).display`);
check('keyboard focus shows tooltip', focusTip === 'block');

const sortCheck = await evaluate(`(() => {
  const table = document.getElementById('throughput-table');
  const header = [...table.querySelectorAll('th')].findIndex(th => th.textContent.trim() === 'Speedup');
  table.querySelectorAll('th')[header].querySelector('button').click();
  const values = [...table.tBodies[0].rows].filter(r => !r.hidden).map(r => parseFloat(r.cells[header].dataset.v));
  const ascending = values.every((v, i) => i === 0 || values[i - 1] <= v);
  table.querySelectorAll('th')[header].querySelector('button').click();
  const values2 = [...table.tBodies[0].rows].filter(r => !r.hidden).map(r => parseFloat(r.cells[header].dataset.v));
  const descending = values2.every((v, i) => i === 0 || values2[i - 1] >= v);
  return {ascending, descending, rows: values.length, aria: table.querySelectorAll('th')[header].getAttribute('aria-sort')};
})()`);
check('table sorts ascending and descending', sortCheck.ascending && sortCheck.descending, JSON.stringify(sortCheck));
const rowsFiltered = await evaluate(`[...document.querySelectorAll('#throughput-table tbody tr')].filter(r => !r.hidden).length`);
await evaluate(`document.getElementById('show-all').click()`);
const rowsAll = await evaluate(`[...document.querySelectorAll('#throughput-table tbody tr')].filter(r => !r.hidden).length`);
check('table filter and show-all toggle', rowsAll > rowsFiltered && rowsFiltered > 0, `${rowsFiltered} -> ${rowsAll}`);
await screenshot('07-tables', '#tables');
await screenshot('08-memory', '#memory');
await screenshot('09-startup', '#startup');
await screenshot('10-hardware', '#hardware');

const legends = await evaluate(`[...document.querySelectorAll('[data-panel] svg')].map(svg => svg.textContent.includes('Before · ') && svg.textContent.includes('After · ')).every(Boolean)`);
check('every chart has a Before/After legend', legends);
const candleLegend = await evaluate(`[...document.querySelectorAll('[data-chart="candlestick"] svg')].every(svg => svg.textContent.includes('Lower whisker: min → p50'))`);
check('every candlestick explains its convention', candleLegend);
const external = await evaluate(`[...document.querySelectorAll('script[src],link[href],img[src]')].filter(e => /^https?:/.test(e.src || e.href)).length`);
check('no external resources', external === 0, String(external));
await send('Emulation.setDeviceMetricsOverride', { width: 420, height: 900, deviceScaleFactor: 1, mobile: true });
await sleep(200);
const overflow = await evaluate(`document.documentElement.scrollWidth <= innerWidth + 1`);
check('no horizontal page overflow at 420 px', overflow);
await screenshot('11-mobile', '#throughput');

const errors = events.filter((e) => e.method === 'Runtime.exceptionThrown' ||
  (e.method === 'Runtime.consoleAPICalled' && e.params.type === 'error') ||
  (e.method === 'Log.entryAdded' && e.params.entry.level === 'error'));
check('no console errors or exceptions', errors.length === 0, errors.map((e) => JSON.stringify(e.params).slice(0, 200)).join('; '));
writeFileSync(join(outDir, 'results.json'), JSON.stringify(results, null, 1));
ws.close();
chrome.kill();
process.exit(results.every((r) => r.ok) ? 0 : 1);
