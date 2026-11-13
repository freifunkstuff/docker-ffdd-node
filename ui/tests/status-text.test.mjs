import assert from 'node:assert/strict';
import test from 'node:test';
import { backboneStatusText, connectionText, meshStatusText, showSelectedGateway } from '../src/status-text.ts';

test('selected upstream gateway is only hidden for the functional gateway role', () => {
  assert.equal(showSelectedGateway('gateway'), false);
  for (const role of ['node', 'server', '', undefined, null, 'unknown']) {
    assert.equal(showSelectedGateway(role), true);
  }
});

test('connection labels are German and distinguish unavailable state', () => {
  assert.equal(connectionText(true), 'Verbunden');
  assert.equal(connectionText(false), 'Getrennt');
  assert.equal(connectionText(undefined), 'Unbekannt');
});

test('mesh retains stabilizing versus stable semantics with German text', () => {
  assert.equal(meshStatusText(true, true), 'Verbunden');
  assert.equal(meshStatusText(true, false), 'Stabilisiert sich');
  assert.equal(meshStatusText(false, false), 'Getrennt');
  assert.equal(meshStatusText(undefined, undefined), 'Unbekannt');
});

test('backbone technical values are translated for display only', () => {
  assert.equal(backboneStatusText('connected'), 'Verbunden');
  assert.equal(backboneStatusText('CONNECTED'), 'Verbunden');
  assert.equal(backboneStatusText('disconnected'), 'Getrennt');
  assert.equal(backboneStatusText('connecting'), 'Verbindungsaufbau');
  assert.equal(backboneStatusText('unknown'), 'Unbekannt');
  assert.equal(backboneStatusText('__proto__'), 'Unbekannt');
});
