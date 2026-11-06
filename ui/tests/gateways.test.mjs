import assert from 'node:assert/strict';
import test from 'node:test';
import { GATEWAY_COLUMNS, gatewayIndicators } from '../src/gateways.ts';

test('selection columns appear between Usage and BRC', () => {
  assert.deepEqual(GATEWAY_COLUMNS.map(column => column.key), [
    'node', 'ip', 'best_next_hop', 'speed', 'usage', 'preferred', 'selected', 'brc',
  ]);
});

const preferred = '10.200.200.21';
const fallback = '10.200.200.19';

test('preferred and active are separate states during fallback', () => {
  const selection = { preferred, selected: fallback };
  assert.deepEqual(gatewayIndicators({ ip: preferred }, selection), { preferred: true, active: false });
  assert.deepEqual(gatewayIndicators({ ip: fallback }, selection), { preferred: false, active: true });
});

test('preferred gateway may be active at the same time', () => {
  assert.deepEqual(gatewayIndicators({ ip: preferred }, { preferred, selected: preferred }), { preferred: true, active: true });
});

test('ordinary gateway has no selection symbol', () => {
  assert.deepEqual(gatewayIndicators({ ip: '10.200.0.1' }, { preferred, selected: fallback }), { preferred: false, active: false });
});

test('missing selection data uses explicit per-row booleans', () => {
  assert.deepEqual(gatewayIndicators({ preferred: true, selected: false }, {}), { preferred: true, active: false });
  assert.deepEqual(gatewayIndicators({}, {}), { preferred: false, active: false });
});

test('aggregate bmxd IP state overrides conflicting or stale row flags', () => {
  assert.deepEqual(gatewayIndicators({ ip: fallback, preferred: true, selected: true }, { preferred, selected: preferred }), { preferred: false, active: false });
  for (const none of ['', '0.0.0.0', '-']) {
    assert.deepEqual(gatewayIndicators({ ip: preferred, preferred: true, selected: true }, { preferred: none, selected: none }), { preferred: false, active: false });
  }
});
