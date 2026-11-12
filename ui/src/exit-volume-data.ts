export type ExitTrafficDay = { date: string; rx_bytes: number; tx_bytes: number; partial: boolean };
export type ExitTrafficPayload = {
  version: 1;
  timezone: 'Europe/Berlin';
  retention_days: 30;
  started_at: number;
  updated_at: number;
  totals: { rx_bytes: number; tx_bytes: number };
  days: ExitTrafficDay[];
};
export type ExitVolumeDay = { date: string; value: ExitTrafficDay | null };

const counter = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value) && value >= 0;
const record = (value: unknown): value is Record<string, unknown> => typeof value === 'object' && value !== null && !Array.isArray(value);
const dateKey = (value: unknown): value is string => {
  if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
  const time = Date.parse(`${value}T00:00:00Z`);
  return Number.isFinite(time) && new Date(time).toISOString().slice(0, 10) === value;
};

/** Copy only public, validated fields; malformed days remain unknown, never zero. */
export function validateExitTrafficPayload(input: unknown): ExitTrafficPayload | null {
  if (!record(input) || input.version !== 1 || input.timezone !== 'Europe/Berlin' || input.retention_days !== 30
    || !counter(input.started_at) || !counter(input.updated_at) || input.started_at > input.updated_at
    || !record(input.totals) || !counter(input.totals.rx_bytes) || !counter(input.totals.tx_bytes)
    || !Number.isFinite(input.totals.rx_bytes + input.totals.tx_bytes)
    || !Array.isArray(input.days) || input.days.length > 30) return null;
  try { new Date(input.updated_at * 1000).toISOString(); } catch { return null; }
  const days = new Map<string, ExitTrafficDay>();
  for (const day of input.days) {
    if (!record(day) || !dateKey(day.date) || !counter(day.rx_bytes) || !counter(day.tx_bytes)
      || !Number.isFinite(day.rx_bytes + day.tx_bytes) || typeof day.partial !== 'boolean') continue;
    days.set(day.date, { date: day.date, rx_bytes: day.rx_bytes, tx_bytes: day.tx_bytes, partial: day.partial });
  }
  return {
    version: 1, timezone: 'Europe/Berlin', retention_days: 30,
    started_at: input.started_at, updated_at: input.updated_at,
    totals: { rx_bytes: input.totals.rx_bytes, tx_bytes: input.totals.tx_bytes },
    days: [...days.values()].sort((a, b) => a.date.localeCompare(b.date)),
  };
}

/** Calendar day is anchored to the server timestamp, not the browser's clock or timezone. */
export function latestExitTrafficDate(payload: ExitTrafficPayload): string {
  const parts = new Intl.DateTimeFormat('en-GB', { timeZone: payload.timezone, year: 'numeric', month: '2-digit', day: '2-digit' }).formatToParts(new Date(payload.updated_at * 1000));
  const get = (type: string) => parts.find(part => part.type === type)!.value;
  return `${get('year')}-${get('month')}-${get('day')}`;
}

/** UTC arithmetic on date keys avoids skipped/repeated days at DST transitions. */
export function exitVolumeDays(input: unknown): ExitVolumeDay[] {
  const payload = validateExitTrafficPayload(input);
  if (!payload) return [];
  const latest = latestExitTrafficDate(payload);
  const end = Date.parse(`${latest}T00:00:00Z`);
  const started = latestExitTrafficDate({ ...payload, updated_at: payload.started_at });
  const known = new Map(payload.days.map(day => [day.date, day]));
  return Array.from({ length: 30 }, (_, index) => {
    const date = new Date(end - (29 - index) * 86400000).toISOString().slice(0, 10);
    return { date, value: date < started ? null : known.get(date) ?? null };
  });
}

export function formatExitVolume(value: number, unit: 'MiB' | 'GiB' = value >= 1024 ** 3 ? 'GiB' : 'MiB'): string {
  return `${(value / 1024 ** (unit === 'GiB' ? 3 : 2)).toLocaleString('de-DE', { maximumFractionDigits: 1 })} ${unit}`;
}
