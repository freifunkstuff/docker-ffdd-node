export interface ExitCounterFrame {
  timestamp?: unknown;
  data?: {
    resources?: { uptime_seconds?: unknown };
    exit_interfaces?: Record<string, { rx_bytes?: unknown; tx_bytes?: unknown; interface?: unknown }>;
  };
  aggregation?: { exit_discontinuous?: Record<string, { rx?: boolean; tx?: boolean }> };
}
export interface ExitSample { timestamp: number; rx: number | null; tx: number | null }
export function exitStateText(state: unknown): string {
  switch (state) {
    case 'disabled': return 'Deaktiviert';
    case 'configuring': return 'Verbindungsaufbau';
    case 'ready': return 'Verbunden';
    case 'unreachable': return 'Nicht erreichbar';
    case 'error': return 'Fehler';
    default: return 'Unbekannt';
  }
}
const finite = (value: unknown): number | null => typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : null;
const timestamp = (value: unknown): number | null => typeof value === 'string' && value.trim() !== '' ? finite(Number(value)) : finite(value);
/** Raw counters only: missing samples, resets and discontinuities never become zero traffic. */
export function exitSeries(frames: readonly ExitCounterFrame[], profileName: string, resolution: number | string): ExitSample[] {
  const step = Number(resolution) === 15 ? 15 : 1;
  const seen = new Set<number>();
  const sorted = frames.flatMap(frame => {
    const time = timestamp(frame.timestamp);
    if (time === null || time <= 0 || seen.has(time)) return [];
    seen.add(time);
    return [{ frame, time }];
  }).sort((a, b) => a.time - b.time);
  const end = sorted[sorted.length - 1]?.time ?? 0;
  const bounded = sorted.filter(item => item.time >= end - (step === 1 ? 300 : 3600)).slice(-500);
  return bounded.map(({ frame, time }, index) => {
    const previous = bounded[index - 1];
    const current = frame.data?.exit_interfaces?.[profileName];
    const old = previous?.frame.data?.exit_interfaces?.[profileName];
    const uptime = finite(frame.data?.resources?.uptime_seconds);
    const oldUptime = finite(previous?.frame.data?.resources?.uptime_seconds);
    const elapsed = previous ? time - previous.time : 0;
    const reset = uptime !== null && oldUptime !== null && uptime < oldUptime;
    const changed = current?.interface !== old?.interface;
    const rate = (key: 'rx' | 'tx') => {
      const value = finite(current?.[`${key}_bytes`]);
      const before = finite(old?.[`${key}_bytes`]);
      if (!previous || elapsed <= 0 || elapsed > 2 * step || reset || changed || frame.aggregation?.exit_discontinuous?.[profileName]?.[key] || value === null || before === null || value < before) return null;
      const result = (value - before) / elapsed;
      return Number.isFinite(result) ? result : null;
    };
    return { timestamp: time, rx: rate('rx'), tx: rate('tx') };
  });
}
