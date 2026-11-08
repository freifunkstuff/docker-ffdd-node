import test from 'node:test';
import assert from 'node:assert/strict';
import { appendStatistics, rebuildStatistics as rebuild, chartSegments, MAX_SAMPLES, nonnegative, linuxKiB } from '../src/statistics-data.ts';

const snapshot = (timestamp, resources, interfaces) => ({ timestamp: String(timestamp), data: { resources, statistic: { interfaces } } });
const resources = (cpu, uptime = cpu) => ({ scope: 'container', cpu_capacity: 2, cpu_total_seconds: cpu, uptime_seconds: uptime, memory_current_bytes: 100, memory_limit_bytes: 200 });
const pair = (a, b) => appendStatistics(appendStatistics([], a), b);
// Older fixtures use thirty-second counter intervals within the coarse view.
const rebuildStatistics = (payload, current) => rebuild(payload, current, '15');
const history = (samples, updated_at = samples.at(-1)?.timestamp ?? '100') => ({ version: 1, history_seconds: 3600, detail_seconds: 300, updated_at, resolutions: { '1': samples, '15': samples } });

test('reload immediately rehydrates CPU and network rates from raw backend frames', () => {
  const frames = [100, 130, 160].map(t => snapshot(t, resources(t, t), { tbb_wg_rx: String(t * 10), tbb_wg_tx: '0' }));
  const samples = rebuildStatistics(history(frames), null);
  assert.deepEqual(samples.map(s => s.cpu), [null, 50, 50]);
  assert.deepEqual(samples.map(s => s.rx), [null, 10, 10]);
  assert.equal(samples.at(-1).tx, 0);
});
test('slow sysinfo never extends or overrides authoritative backend history', () => {
  const frames = [100, 130].map(t => snapshot(t, resources(t, t)));
  const samples = rebuildStatistics(history(frames), snapshot(160, resources(160, 160)));
  assert.deepEqual(samples.map(s => s.timestamp), [100, 130]);
  assert.equal(samples.at(-1).cpu, 50);
  assert.equal(rebuildStatistics(history(frames), snapshot(130, resources(999))).at(-1).cpuTotal, 130);
  assert.equal(rebuildStatistics(history(frames), snapshot(110, resources(999))).length, 2);
});
test('history sorts valid frames deterministically and first backend duplicate wins', () => {
  const a = snapshot(100, resources(100, 100));
  const b = snapshot(130, resources(130, 130));
  const c = snapshot(160, resources(160, 160));
  const payload = history([c, { timestamp: 'bad' }, a, b, snapshot(130, resources(999)), { timestamp: '0' }]);
  assert.deepEqual(rebuildStatistics(payload, null), rebuildStatistics(history([a, b, c]), null));
});
test('shorter authoritative collections discard prior frontend/node history and retain real reset gaps', () => {
  const before = snapshot(100, resources(100, 100));
  const reboot = snapshot(130, resources(1, 1));
  const next = snapshot(160, resources(31, 31));
  assert.deepEqual(rebuildStatistics(history([before, reboot, next]), null).map(s => s.cpu), [null, null, 50]);
  assert.deepEqual(rebuildStatistics(history([reboot]), null).map(s => s.timestamp), [130]);
  assert.equal(rebuildStatistics(history([reboot]), before).length, 1);
  // A previous-boot live cache can be ahead after a wall-clock correction.
  assert.equal(rebuildStatistics(history([snapshot(90, resources(1, 1))]), before).length, 1);
});
test('empty and single-frame boot collections never fabricate older data or initial rates', () => {
  assert.deepEqual(rebuildStatistics(history([]), null), []);
  assert.deepEqual(rebuildStatistics(history([], '130'), snapshot(100, resources(100))), []);
  const samples = rebuildStatistics(history([snapshot(100, resources(1, 1))]), null);
  assert.equal(samples.length, 1);
  assert.equal(samples[0].cpu, null);
  assert.equal(samples[0].rx, null);
  assert.equal(samples[0].memory, 100);
});
test('rehydrated history remains bounded by one hour and count', () => {
  const frames = Array.from({ length: 2000 }, (_, i) => snapshot(i + 1, resources(i + 1)));
  assert.equal(rebuildStatistics(history(frames), null).length, MAX_SAMPLES);
  assert.equal(rebuildStatistics(history([...frames, snapshot(10000, resources(10000))]), null).length, 1);
});

test('resolution selects only its real frames and bounds detail to five minutes and coarse to one hour', () => {
  const payload = history([]);
  payload.resolutions['1'] = [100, 101, 400, 401].map(t => snapshot(t, resources(t)));
  payload.resolutions['15'] = [1, 1000, 3985, 4000].map(t => snapshot(t, resources(t)));
  assert.deepEqual(rebuild(payload, null).map(s => s.timestamp), [101, 400, 401]);
  assert.deepEqual(rebuild(payload, null, '15').map(s => s.timestamp), [1000, 3985, 4000]);
  assert.equal(rebuild(payload, null).at(-1).cpu, 50);
  assert.equal(rebuild(payload, null, '15').at(-1).cpu, 50);
});
test('discontinuous aggregate and collection gaps leave unknown rate intervals', () => {
  const frames = [100, 115, 130, 175, 190].map(t => snapshot(t, resources(t), { tbb_wg_rx: String(t), tbb_wg_tx: String(t) }));
  frames[1].aggregation = { count: 15, start: 101, end: 115, discontinuous: true };
  const samples = rebuild(history(frames), null, '15');
  assert.deepEqual(samples.map(s => s.cpu), [null, null, null, null, 50]);
  assert.deepEqual(samples.map(s => s.rx), [null, null, null, null, 1]);
  assert.equal(samples[1].memory, 100);
  const detail = rebuild(history([snapshot(100, resources(100)), snapshot(103, resources(103))]), null);
  assert.equal(detail[1].cpu, null);
});

test('per-metric aggregate discontinuities never suppress unrelated healthy rates', () => {
  for (const metric of ['cpu', 'rx', 'tx']) {
    const frames = [100, 115, 130, 145].map(t => snapshot(t, resources(t), { tbb_wg_rx: String(t), tbb_wg_tx: String(t) }));
    frames[1].aggregation = { count: 15, start: 101, end: 115, discontinuous: false, [`${metric}_discontinuous`]: true };
    const samples = rebuild(history(frames), null, '15');
    for (const key of ['cpu', 'rx', 'tx']) {
      const rate = key === 'cpu' ? 50 : 1;
      assert.deepEqual(samples.map(s => s[key]), key === metric ? [null, null, null, rate] : [null, rate, rate, rate]);
    }
  }
});

test('CPU uses total delta, elapsed uptime and capacity, never host load', () => {
  const samples = pair(snapshot(100, resources(10, 100)), snapshot(130, resources(40, 130)));
  assert.equal(samples[0].cpu, null);
  assert.equal(samples[1].cpu, 50);
  assert.equal(appendStatistics([], { timestamp: '100', data: { statistic: { cpu_load: '9' } } })[0].cpu, null);
});
test('CPU reset, reboot and capacity/scope changes leave gaps', () => {
  const a = snapshot(100, resources(10, 100));
  for (const r of [resources(5, 130), resources(40, 2), { ...resources(40, 130), cpu_capacity: 1 }, { ...resources(40, 130), scope: 'host' }]) {
    assert.equal(pair(a, snapshot(130, r))[1].cpu, null);
  }
});
test('positive timestamp fallback and uptime guard handle clock changes', () => {
  assert.equal(pair(snapshot(100, { scope: 'container', cpu_capacity: 1, cpu_total_seconds: 0 }), snapshot(130, { scope: 'container', cpu_capacity: 1, cpu_total_seconds: 15 }))[1].cpu, 50);
  assert.equal(pair(snapshot(100, resources(0, 100)), snapshot(400, resources(15, 130)))[1].cpu, 25);
  const first = appendStatistics([], snapshot(100, resources(0)));
  assert.equal(appendStatistics(first, snapshot(90, resources(2))), first);
});
test('duplicate and invalid timestamps never add samples', () => {
  const first = appendStatistics([], snapshot(100, resources(10)));
  assert.equal(appendStatistics(first, snapshot(100, resources(20))), first);
  for (const timestamp of ['', 'nonsense', '0']) assert.equal(appendStatistics(first, { timestamp }), first);
});
test('legacy memory is actual Linux kB, not a host estimate', () => {
  const [s] = appendStatistics([], { timestamp: '100', data: { statistic: { meminfo_MemTotal: '204800 kB', meminfo_MemFree: '102400 kB' } } });
  assert.equal(s.memoryLimit, 200 * 1024 ** 2);
  assert.equal(s.memory, 100 * 1024 ** 2);
  assert.equal(appendStatistics([], snapshot(100, { scope: 'container' }))[0].memory, null);
});
test('strict numeric and Linux KiB parsing rejects missing or fabricated values', () => {
  for (const value of [[], {}, true, null, undefined, '', ' ', Infinity, -1]) assert.equal(nonnegative(value), null);
  for (const value of ['204800', '204800 kB', '204800 KiB']) assert.equal(linuxKiB(value), 204800);
  for (const value of ['204800 MB', '204800 B', 'unknown', [], {}]) assert.equal(linuxKiB(value), null);
});
test('unknown CPU scope cannot supply utilization', () => {
  for (const scope of [undefined, 'unknown', 'cgroup']) {
    assert.equal(pair(snapshot(100, { ...resources(10, 100), scope }), snapshot(130, { ...resources(40, 130), scope }))[1].cpu, null);
  }
});
test('legacy uptime parses proc tokens but never supplies resource uptime', () => {
  const legacy = { timestamp: '100', data: { system: { uptime: '123.45 678.90' } } };
  assert.equal(appendStatistics([], legacy)[0].uptime, 123.45);
  const a = snapshot(100, { ...resources(10), uptime_seconds: undefined });
  const b = snapshot(130, { ...resources(40), uptime_seconds: undefined });
  a.data.system = { uptime: '999 888' };
  b.data.system = { uptime: '1 2' };
  const samples = pair(a, b);
  assert.equal(samples[1].uptime, null);
  assert.equal(samples[1].cpu, 50);
});
test('backbone sums WG and fastd with actual zeros and no underlay', () => {
  const a = { tbb_wg_rx: '100', tbb_wg_tx: '0', tbb_fastd_rx: '20', tbb_fastd_tx: '0', tbbwg_rx: '99999' };
  const b = { ...a, tbb_wg_rx: '400', tbb_fastd_rx: '80' };
  const samples = pair(snapshot(100, undefined, a), snapshot(130, undefined, b));
  assert.equal(samples[1].rx, 12);
  assert.equal(samples[1].tx, 0);
  assert.equal(pair(snapshot(100), snapshot(130))[1].rx, null);
  assert.equal(pair(snapshot(100, undefined, b), snapshot(130, undefined, a))[1].rx, null);
  assert.equal(pair(snapshot(100, undefined, a), snapshot(130, undefined, { tbb_wg_rx: '400' }))[1].rx, null);
});
test('history is time and count bounded', () => {
  let samples = [];
  for (let i = 1; i <= 2000; i++) samples = appendStatistics(samples, snapshot(i, resources(i)));
  assert.equal(samples.length, MAX_SAMPLES);
  samples = appendStatistics(samples, snapshot(10000, resources(10000)));
  assert.equal(samples.length, 1);
});
test('chart geometry preserves gaps and singleton dots within bounds', () => {
  const samples = [0, null, 50, 100].map((cpu, i) => ({ timestamp: 100 + i, cpu }));
  const segments = chartSegments(samples, 'cpu', 100, 103, 100);
  assert.deepEqual(segments.map(s => s.length), [1, 2]);
  assert.deepEqual(segments[0][0], { x: 0, y: 1 });
  assert.deepEqual(segments[1][1], { x: 1, y: 0 });
});
