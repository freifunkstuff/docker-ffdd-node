import assert from 'node:assert/strict';
import test from 'node:test';
import { matchesNodeFilter } from '../src/node-filter.ts';

const row = { node: '913', ip: '10.200.3.149', interface: 'mesh0', rtq: '100', rq: '99', tq: '98', best_next_hop: '10.201.200.21', brc: '777', speed: '50Mbit', usage: '12%', type: 'gateway' };

test('Nodes filter matches only ID and own IP, including partial input', () => {
  for (const query of ['913', ' 913 ', '91', '10.200.3', '149', '']) assert.equal(matchesNodeFilter(row, query), true, query);
  assert.equal(matchesNodeFilter({ node: 913 }, '913'), true);
});

test('Nodes filter ignores interfaces, quality, next hop and other columns', () => {
  for (const query of ['mesh0', '100', '99', '98', '10.201', '777', '50Mbit', '12%', 'gateway']) assert.equal(matchesNodeFilter(row, query), false, query);
});

test('Nodes filter handles absent fields and whitespace', () => {
  assert.equal(matchesNodeFilter({}, '123'), false);
  assert.equal(matchesNodeFilter({}, '   '), true);
  assert.equal(matchesNodeFilter({ node: null, ip: null }, 'null'), false);
});
