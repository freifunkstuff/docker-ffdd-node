import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';
import { isThemePreference, readThemePreference, resolveTheme, writeThemePreference, THEME_STORAGE_KEY } from '../src/theme.ts';

test('light is default, including absent, invalid or blocked storage', () => {
  assert.equal(readThemePreference(null), 'light');
  assert.equal(readThemePreference({ getItem: () => 'invalid' }), 'light');
  assert.equal(readThemePreference({ getItem: () => { throw new Error('blocked'); } }), 'light');
});

test('explicit preference wins over operating-system scheme', () => {
  assert.equal(resolveTheme('light', true), 'light');
  assert.equal(resolveTheme('dark', false), 'dark');
  assert.equal(resolveTheme('system', true), 'dark');
  assert.equal(resolveTheme('system', false), 'light');
});

test('only the supported theme choices are accepted', () => {
  for (const value of ['light', 'dark', 'system']) assert.ok(isThemePreference(value));
  for (const value of ['', null, undefined, 'DARK']) assert.equal(isThemePreference(value), false);
});

test('theme preference persists without making storage mandatory', () => {
  const values = new Map();
  const storage = { getItem: key => values.get(key) ?? null, setItem: (key, value) => values.set(key, value) };
  writeThemePreference(storage, 'dark');
  assert.equal(values.get(THEME_STORAGE_KEY), 'dark');
  assert.equal(readThemePreference(storage), 'dark');
  assert.doesNotThrow(() => writeThemePreference({ setItem: () => { throw new Error('blocked'); } }, 'light'));
});

test('existing navigation and extension integration remain, statistics is additive', () => {
  const source = readFileSync(new URL('../src/App.tsx', import.meta.url), 'utf8');
  for (const [key, label] of [['status', 'Status & Kontakt'], ['nodes', 'Nodes'], ['licenses', 'Rechtliches'], ['statistics', 'Statistik']]) {
    assert.ok(source.includes(`key: '${key}', label: '${label}'`));
  }
  assert.ok(source.includes("const EXTENSIONS_INDEX_URL = '/ui/extensions/index.json'"));
  assert.ok(source.includes('[...CORE_NAV_ITEMS, ...extensionNavItems]'));
  assert.ok(source.includes('Freifunk {communityName}'));
  const sidebar = source.split('<aside class="sidebar">')[1].split('</aside>')[0];
  assert.equal(sidebar.includes('common.node'), false);
  assert.equal(sidebar.includes('titleName'), false);
});
