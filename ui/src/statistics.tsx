import { useEffect, useMemo, useState } from 'preact/hooks';
import type { StatisticsHistoryPayload, StatisticsResolution, SysinfoPayload } from './types';
import { appendStatistics, rebuildStatistics, chartSegments, HISTORY_SECONDS } from './statistics-data';
import type { StatisticsSample } from './statistics-data';
import './statistics.css';

export function useStatistics(sysinfo: SysinfoPayload | null, history?: StatisticsHistoryPayload | null, resolution: StatisticsResolution = '1'): StatisticsSample[] {
  const [samples, setSamples] = useState<StatisticsSample[]>([]);
  const restored = useMemo(() => history ? rebuildStatistics(history, sysinfo, resolution) : null, [history, sysinfo, resolution]);
  useEffect(() => {
    setSamples((previous) => restored ?? appendStatistics(previous, sysinfo));
  }, [sysinfo, restored]);
  return restored ?? samples;
}

function bytes(value: number): string {
  return `${(value / 1024 ** 2).toFixed(1)} MiB`;
}
const clock = (timestamp: number) => new Date(timestamp * 1000).toLocaleTimeString('de-DE', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
type Metric = 'cpu' | 'memory' | 'rx' | 'tx';
type Series = { key: Metric; label: string; className: string };

function Chart({ title, samples, series, maximum, format, limit }: {
  title: string; samples: readonly StatisticsSample[]; series: Series[];
  maximum: number; format: (value: number) => string; limit?: number | null;
}) {
  const last = samples[samples.length - 1];
  const end = last?.timestamp ?? 0;
  const start = samples.length > 1 ? Math.max(samples[0].timestamp, end - HISTORY_SECONDS) : end - 60;
  const available = series.some(({ key }) => samples.some((sample) => sample[key] !== null));
  const waitingForRate = !!last && series.some(({ key }) => key === 'cpu'
    ? last.cpuTotal !== null && last.capacity !== null && last.capacity > 0
    : (key === 'rx' || key === 'tx') && Object.entries(last.counters).some(([name, value]) => name.endsWith(`_${key}`) && value !== null));
  return <section class="statistics-panel" aria-label={title}>
    <h4>{title}</h4>
    <div class="statistics-values">{series.map(({ key, label, className }) => <span class={className} key={key}>
      {series.length > 1 && <span>{label} </span>}{last?.[key] == null ? '—' : format(last[key]!)}
    </span>)}{limit != null && limit > 0 && <small class="statistics-limit"> / {bytes(limit)}</small>}</div>
    <div class="statistics-plot">
      <svg viewBox="0 0 360 190" role="img" aria-label={`${title}: zeitlicher Verlauf`}>
        {[0, 0.5, 1].map((fraction) => <g key={fraction}>
          <line class="statistics-gridline" x1="66" x2="348" y1={12 + fraction * 146} y2={12 + fraction * 146} />
          <text class="statistics-axis" x="60" y={16 + fraction * 146} text-anchor="end">{format(maximum * (1 - fraction))}</text>
        </g>)}
        {series.map(({ key, className }) => <g class={className} key={key}>
          {chartSegments(samples, key, start, end, maximum).map((segment, i) => <g key={i}>
            {segment.length > 1 && <polyline class="statistics-line" points={segment.map(({ x, y }) => `${66 + x * 282},${12 + y * 146}`).join(' ')} />}
            {segment.length === 1 && <circle cx={66 + segment[0].x * 282} cy={12 + segment[0].y * 146} r="3" />}
          </g>)}
        </g>)}
        {last && <><text class="statistics-axis" x="66" y="181">{clock(start)}</text><text class="statistics-axis" x="348" y="181" text-anchor="end">{clock(end)}</text></>}
        {!available && <text class="statistics-empty" x="207" y="85" text-anchor="middle" dominant-baseline="middle">{waitingForRate ? 'Sammle Messwerte …' : 'Keine Messwerte'}</text>}
      </svg>
    </div>
  </section>;
}

export function StatisticsPage({ samples: source, resolution = '1', onResolutionChange }: {
  samples: readonly StatisticsSample[];
  resolution?: StatisticsResolution;
  onResolutionChange?: (resolution: StatisticsResolution) => void;
}) {
  const window = resolution === '1' ? 300 : HISTORY_SECONDS;
  const end = source[source.length - 1]?.timestamp ?? 0;
  const samples = source.filter(sample => sample.timestamp >= end - window);
  const last = samples[samples.length - 1];
  const peak = (keys: Metric[]) => Math.max(1, ...samples.flatMap((sample) => keys.map((key) => sample[key] ?? 0)));
  return <section class="statistics-page">
    <div class="statistics-heading"><h3>Statistik</h3>
      <div class="statistics-resolution" role="group" aria-label="Zeitraum">
        {(['1', '15'] as const).map(value => <button type="button" key={value} aria-pressed={resolution === value} onClick={() => onResolutionChange?.(value)}>{value === '1' ? '5 Min' : '1h'}</button>)}
      </div>
    </div>
    <div class="statistics-grid">
      <Chart title="CPU" samples={samples} series={[{ key: 'cpu', label: 'CPU', className: 'statistics-primary' }]} maximum={Math.max(100, peak(['cpu']))} format={(value) => `${value.toFixed(1)} %`} />
      <Chart title="RAM" samples={samples} series={[{ key: 'memory', label: 'RAM', className: 'statistics-primary' }]} maximum={last?.memoryLimit || Math.max(1024 ** 2, peak(['memory']))} limit={last?.memoryLimit} format={bytes} />
      <Chart title="Backbone traffic" samples={samples} series={[{ key: 'rx', label: 'RX', className: 'statistics-primary' }, { key: 'tx', label: 'TX', className: 'statistics-secondary' }]} maximum={Math.max(125000, peak(['rx', 'tx']))} format={(value) => `${(value * 8 / 1000000).toFixed(2)} Mbit/s`} />
    </div>
  </section>;
}
