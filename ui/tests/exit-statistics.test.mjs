import test from 'node:test';
import assert from 'node:assert/strict';
import { exitSeries, exitStateText } from '../src/exit-statistics.ts';
const frame = (timestamp, rx = 10, tx = 20, uptime = timestamp) => ({ timestamp, data: { resources: { uptime_seconds: uptime }, exit_interfaces: { a: { rx_bytes: rx, tx_bytes: tx } } } });
test('isolated exit counters, ordered timestamps, valid zero and warmup', () => {
  assert.deepEqual(exitSeries([frame(2, 20, 20), frame(1)], 'a', '1'), [{ timestamp: 1, rx: null, tx: null }, { timestamp: 2, rx: 10, tx: 0 }]);
  assert.ok(exitSeries([frame(1), frame(2)], 'missing', 1).every(s => s.rx === null && s.tx === null));
});
test('strict counters reject strings, negatives, nonfinite, missing without fallback', () => {
  for (const value of ['20', -1, Infinity, NaN, null, undefined]) {
    const invalid = frame(2); invalid.data.exit_interfaces.a.rx_bytes = value;
    assert.equal(exitSeries([frame(1), invalid], 'a', 1)[1].rx, null);
  }
  const missing = { timestamp: 2, data: { statistic: { interfaces: { tbb_wg_rx: 100 } } } };
  assert.equal(exitSeries([frame(1), missing, frame(3)], 'a', 1)[2].rx, null);
});
test('counter resets, restart, interface replacement and long gaps interrupt lines', () => {
  assert.equal(exitSeries([frame(1, 50), frame(2, 20)], 'a', 1)[1].rx, null);
  assert.equal(exitSeries([frame(1, 10, 20, 100), frame(2, 20, 30, 1)], 'a', 1)[1].tx, null);
  assert.equal(exitSeries([frame(1), frame(4, 40)], 'a', 1)[1].rx, null);
  const changed = frame(2, 20); changed.data.exit_interfaces.a.interface = 'new';
  assert.equal(exitSeries([frame(1), changed], 'a', 1)[1].rx, null);
});
test('coarse series respects per-direction discontinuity flags', () => {
  const current = frame(30, 40, 50); current.aggregation = { exit_discontinuous: { a: { rx: true, tx: false } } };
  const result = exitSeries([frame(15), current], 'a', '15')[1];
  assert.equal(result.rx, null); assert.equal(result.tx, 2);
});
test('bounds, timestamps and first duplicate wins', () => {
  const frames = Array.from({ length: 4000 }, (_, i) => frame(i + 1));
  assert.equal(exitSeries(frames, 'a', 1).length, 301);
  assert.ok(exitSeries(frames, 'a', 15).length <= 500);
  assert.deepEqual(exitSeries([{ timestamp: 'bad' }, frame(0), frame(1), frame(1, 100), frame('2', 20)], 'a', 1).map(s => s.rx), [null, 10]);
});
test('German states do not echo untrusted values', () => {
  assert.equal(exitStateText('ready'), 'Verbunden'); assert.equal(exitStateText('error'), 'Fehler');
  assert.equal(exitStateText('secret input'), 'Unbekannt');
});
