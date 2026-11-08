import type { StatisticsHistoryPayload, StatisticsResolution, SysinfoPayload } from './types.ts';

export const HISTORY_SECONDS = 60 * 60;
export const MAX_SAMPLES = 1800;
type Counters = Record<string, number | null>;
export interface StatisticsSample {
  timestamp: number;
  cpu: number | null;
  memory: number | null;
  memoryLimit: number | null;
  rx: number | null;
  tx: number | null;
  scope: string | undefined;
  capacity: number | null;
  cpuTotal: number | null;
  uptime: number | null;
  counters: Counters;
  aggregation?: SysinfoPayload['aggregation'];
}

export function nonnegative(value: unknown): number | null {
  if ((typeof value !== 'number' && typeof value !== 'string') || (typeof value === 'string' && !value.trim())) return null;
  const n = Number(value);
  return Number.isFinite(n) && n >= 0 ? n : null;
}

export function linuxKiB(value: unknown): number | null {
  if (typeof value === 'number') return nonnegative(value);
  if (typeof value !== 'string') return null;
  const match = value.trim().match(/^(\d+(?:\.\d+)?)\s*(?:kB|KiB)?$/);
  return match ? nonnegative(match[1]) : null;
}

function legacyUptime(value: unknown): number | null {
  return typeof value === 'string' ? nonnegative(value.trim().split(/\s+/)[0]) : nonnegative(value);
}

function timestampOf(value: unknown): number | null {
  const numeric = nonnegative(value);
  if (numeric !== null) return numeric > 0 ? numeric : null;
  const parsed = typeof value === 'string' ? Date.parse(value) / 1000 : NaN;
  return Number.isFinite(parsed) && parsed > 0 ? parsed : null;
}

function rate(previous: Counters, current: Counters, direction: string, elapsed: number | null): number | null {
  if (!elapsed) return null;
  let sum = 0;
  let found = false;
  for (const transport of ['wg', 'fastd']) {
    const key = `tbb_${transport}_${direction}`;
    const a = previous[key];
    const b = current[key];
    if (a == null && b == null) continue;
    // Do not bridge an appearing/disappearing transport or a counter reset.
    if (a == null || b == null || b < a) return null;
    sum += b - a;
    found = true;
  }
  return found ? sum / elapsed : null;
}

export function appendStatistics(samples: readonly StatisticsSample[], sysinfo: SysinfoPayload | null): StatisticsSample[] {
  const timestamp = timestampOf(sysinfo?.timestamp);
  if (!sysinfo || timestamp === null || samples.some((sample) => sample.timestamp === timestamp)) return samples as StatisticsSample[];
  const previous = samples[samples.length - 1];
  // Reject out-of-order snapshots rather than inventing intervals after an NTP jump.
  if (previous && timestamp <= previous.timestamp) return samples as StatisticsSample[];
  const resources = sysinfo.data?.resources;
  const legacy = sysinfo.data?.statistic;
  const uptime = resources ? nonnegative(resources.uptime_seconds) : legacyUptime(sysinfo.data?.system?.uptime);
  const wallElapsed = previous ? timestamp - previous.timestamp : null;
  const uptimeElapsed = previous?.uptime != null && uptime !== null ? uptime - previous.uptime : null;
  const aggregation = sysinfo.aggregation;
  const blocked = (metric: 'cpu' | 'rx' | 'tx') => aggregation?.discontinuous || previous?.aggregation?.discontinuous
    || aggregation?.[`${metric}_discontinuous`] || previous?.aggregation?.[`${metric}_discontinuous`];
  const elapsed = aggregation?.discontinuous || previous?.aggregation?.discontinuous ? null
    : uptimeElapsed !== null ? (uptimeElapsed > 0 ? uptimeElapsed : null) : wallElapsed;
  const scope = resources?.scope;
  const capacity = nonnegative(resources?.cpu_capacity);
  const cpuTotal = nonnegative(resources?.cpu_total_seconds);
  const sameScope = !!previous && previous.scope === scope;
  const cpu = !blocked('cpu') && (scope === 'container' || scope === 'host') && sameScope && elapsed && capacity && previous.capacity === capacity && cpuTotal !== null && previous.cpuTotal !== null && cpuTotal >= previous.cpuTotal
    ? (cpuTotal - previous.cpuTotal) / elapsed / capacity * 100 : null;
  let memory = nonnegative(resources?.memory_current_bytes);
  let memoryLimit = nonnegative(resources?.memory_limit_bytes);
  // Legacy sysinfo reports Linux kB, including its cgroup-adjusted memory values.
  if (!resources) {
    const total = linuxKiB(legacy?.meminfo_MemTotal);
    const free = linuxKiB(legacy?.meminfo_MemFree);
    memoryLimit = total !== null ? total * 1024 : null;
    memory = total !== null && free !== null && free <= total ? (total - free) * 1024 : null;
  }
  const counters: Counters = {};
  for (const transport of ['wg', 'fastd']) for (const direction of ['rx', 'tx']) {
    const key = `tbb_${transport}_${direction}` as 'tbb_wg_rx' | 'tbb_wg_tx' | 'tbb_fastd_rx' | 'tbb_fastd_tx';
    counters[key] = nonnegative(legacy?.interfaces?.[key]);
  }
  const sample: StatisticsSample = {
    timestamp, cpu, memory, memoryLimit, scope, capacity, cpuTotal, uptime, counters, aggregation,
    rx: previous && sameScope && !blocked('rx') ? rate(previous.counters, counters, 'rx', elapsed) : null,
    tx: previous && sameScope && !blocked('tx') ? rate(previous.counters, counters, 'tx', elapsed) : null,
  };
  return [...samples.filter((point) => point.timestamp >= timestamp - HISTORY_SECONDS), sample].slice(-MAX_SAMPLES);
}

/** Backend history is authoritative, including when a restart returns a shorter ring.
 * Sort raw frames before deriving rates; the first backend frame wins duplicates.
 * Parent merges the 1-second tail first; slow sysinfo never extends backend history.
 */
export function rebuildStatistics(history: StatisticsHistoryPayload | null | undefined, current: SysinfoPayload | null, resolution: StatisticsResolution = '1'): StatisticsSample[] {
  const frames = new Map<number, SysinfoPayload>();
  for (const frame of history?.resolutions[resolution] ?? []) {
    const timestamp = timestampOf(frame?.timestamp);
    if (timestamp !== null && !frames.has(timestamp)) frames.set(timestamp, frame);
  }
  const ordered = [...frames.entries()].sort(([a], [b]) => a - b);
  const latest = ordered[ordered.length - 1];
  const currentTimestamp = timestampOf(current?.timestamp);
  if (!history && current && currentTimestamp !== null && (!latest || currentTimestamp > latest[0])) {
    ordered.push([currentTimestamp, current]);
  }
  const end = ordered[ordered.length - 1]?.[0];
  const window = resolution === '1' ? 300 : HISTORY_SECONDS;
  const bounded = end === undefined ? [] : ordered.filter(([timestamp]) => timestamp >= end - window).slice(-MAX_SAMPLES);
  return bounded.reduce<StatisticsSample[]>((samples, [timestamp, frame]) => {
    const previous = samples[samples.length - 1];
    const next = appendStatistics(samples, frame);
    // Missing collection intervals are gaps, not a smoothed invented rate.
    if (previous && timestamp - previous.timestamp > Number(resolution) * 2) {
      next[next.length - 1] = { ...next[next.length - 1], cpu: null, rx: null, tx: null };
    }
    return next;
  }, []);
}

export interface ChartPoint { x: number; y: number }
/** Separate segments retain unknown intervals; singleton segments are drawn as dots. */
export function chartSegments(samples: readonly StatisticsSample[], key: 'cpu' | 'memory' | 'rx' | 'tx', start: number, end: number, maximum: number): ChartPoint[][] {
  const segments: ChartPoint[][] = [];
  let segment: ChartPoint[] = [];
  for (const sample of samples) {
    const value = sample[key];
    if (value === null) { if (segment.length) segments.push(segment); segment = []; continue; }
    segment.push({ x: Math.max(0, Math.min(1, (sample.timestamp - start) / Math.max(1, end - start))), y: 1 - Math.max(0, Math.min(1, value / Math.max(1, maximum))) });
  }
  if (segment.length) segments.push(segment);
  return segments;
}
