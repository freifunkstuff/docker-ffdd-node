export type ThemePreference = 'light' | 'dark' | 'system';
export type ResolvedTheme = 'light' | 'dark';
export type ThemeStorage = Pick<Storage, 'getItem' | 'setItem'>;

export const THEME_STORAGE_KEY = 'ffdd-node.theme';

export function isThemePreference(value: unknown): value is ThemePreference {
  return value === 'light' || value === 'dark' || value === 'system';
}

export function readThemePreference(storage?: ThemeStorage | null): ThemePreference {
  try {
    const value = storage?.getItem(THEME_STORAGE_KEY);
    return isThemePreference(value) ? value : 'light';
  } catch {
    return 'light';
  }
}

export function writeThemePreference(storage: ThemeStorage | null, preference: ThemePreference): void {
  try { storage?.setItem(THEME_STORAGE_KEY, preference); } catch { /* optional persistence */ }
}

export function resolveTheme(preference: ThemePreference, systemDark: boolean): ResolvedTheme {
  return preference === 'system' ? (systemDark ? 'dark' : 'light') : preference;
}
