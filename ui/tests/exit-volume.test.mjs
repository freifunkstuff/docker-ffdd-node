import test from 'node:test';
import assert from 'node:assert/strict';
import { exitVolumeDays, latestExitTrafficDate, validateExitTrafficPayload, formatExitVolume } from '../src/exit-volume-data.ts';

const timestamp = date => Date.parse(date) / 1000;
const day = (date, rx_bytes = 0, tx_bytes = 0, partial = false) => ({ date, rx_bytes, tx_bytes, partial });
const payload = (overrides = {}) => ({ version: 1, timezone: 'Europe/Berlin', retention_days: 30, started_at: timestamp('2025-03-29T00:00:00Z'), updated_at: timestamp('2025-03-31T22:30:00Z'), totals: { rx_bytes: 0, tx_bytes: 0 }, days: [], ...overrides });

test('uses Berlin calendar date at server timestamp, including DST boundaries', () => {
  assert.equal(latestExitTrafficDate(payload()), '2025-04-01');
  for (const updated_at of [timestamp('2025-03-30T22:30:00Z'), timestamp('2025-10-26T23:30:00Z')]) {
    const days = exitVolumeDays(payload({ updated_at }));
    assert.equal(days.length, 30);
    assert.equal(new Set(days.map(day => day.date)).size, 30);
    for (let i = 1; i < 30; i++) assert.equal(Date.parse(days[i].date) - Date.parse(days[i - 1].date), 86400000);
  }
});

test('unknown dates are not zero; known zero and partial days survive', () => {
  const days = exitVolumeDays(payload({ days: [day('2025-03-28', 999), day('2025-03-29'), day('2025-03-31', 10, 20, true)] }));
  assert.equal(days.find(d => d.date === '2025-03-28').value, null);
  assert.equal(days.find(d => d.date === '2025-03-29').value.rx_bytes, 0);
  assert.equal(days.find(d => d.date === '2025-03-30').value, null);
  assert.equal(days.find(d => d.date === '2025-03-31').value.partial, true);
  assert.equal(days.at(-1).date, '2025-04-01');
});

test('validates envelopes, bounds arrays, finite strict counters and dates', () => {
  for (const input of [null, {}, payload({ days: Array(31).fill(day('2025-03-31')) }), payload({ days: {} }), payload({ updated_at: Infinity }), payload({ timezone: 'UTC' }), payload({ totals: { rx_bytes: '1', tx_bytes: 0 } })]) {
    assert.equal(validateExitTrafficPayload(input), null);
    assert.deepEqual(exitVolumeDays(input), []);
  }
  const clean = validateExitTrafficPayload(payload({ days: [day('2025-02-30'), day('2025-03-30', NaN), day('2025-03-30', -1), day('2025-03-30', '0'), day('2025-03-30', 0, Infinity), day('2025-03-30', 1e308, 1e308), day('2025-03-31', 5)] }));
  assert.deepEqual(clean.days, [day('2025-03-31', 5)]);
});

test('deduplicates dates and copies only public fields', () => {
  const clean = validateExitTrafficPayload(payload({ secret: 'private', days: [{ ...day('2025-03-31', 2), profile: 'private' }, day('2025-03-31', 3)] }));
  assert.deepEqual(clean.days, [day('2025-03-31', 3)]);
  assert.equal('secret' in clean, false);
  assert.equal('profile' in clean.days[0], false);
});

test('chooses binary display units without touching counters', () => {
  assert.equal(formatExitVolume(1024 ** 2), '1 MiB');
  assert.equal(formatExitVolume(1024 ** 3), '1 GiB');
  assert.equal(formatExitVolume(0), '0 MiB');
});
