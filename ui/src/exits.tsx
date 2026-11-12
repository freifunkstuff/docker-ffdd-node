import { exitSeries, exitStateText } from './exit-statistics';
import type { ExitCounterFrame, ExitSample } from './exit-statistics';
import './statistics.css';
import './exits.css';

export interface ExitStatusPayload {
  timestamp?: number | string;
  enabled: boolean;
  active?: string | null;
  exits: Array<{
    name: string; comment?: string; interface?: string; active: boolean;
    state: 'disabled' | 'configuring' | 'ready' | 'unreachable' | 'error';
    endpoint?: string; handshake_age_seconds?: number | null;
    reachable?: boolean | null; rx_bytes?: number | null; tx_bytes?: number | null;
  }>;
}
const text = (value: string | undefined) => value?.slice(0, 240);
export function ExitStatusPanel({ payload }: { payload: ExitStatusPayload | null }) {
  if (!payload || (!payload.enabled && !payload.exits.length)) return null;
  return <section class="panel exits-status" aria-label="Internet-Ausleitung">
    <h3>Internet-Ausleitung</h3>
    {!payload.exits.length ? <p>Exit-Status nicht verfügbar.</p> : <ul class="exits-list">{payload.exits.slice(0, 8).map(exit => <li key={exit.name}>
      <div class="exit-identity"><strong>{text(exit.name)}</strong>{exit.comment && <span class="exit-comment">{text(exit.comment)}</span>}</div>
      <div class="exit-details"><span class="status-pill identity-unknown">{exit.active ? 'Aktiv' : 'Bereitschaft'}</span><span class={`status-pill ${!exit.active ? 'identity-unknown' : exit.state === 'ready' ? 'connected' : exit.state === 'configuring' ? 'stale' : 'disconnected'}`} title={exit.interface ? `${text(exit.interface)} · ${text(exit.endpoint) || ''}` : undefined}>{exit.active ? exitStateText(exit.state) : 'Nicht geprüft'}</span></div>
    </li>)}</ul>}
  </section>;
}
const format = (value: number) => `${(value * 8 / 1e6).toFixed(2)} Mbit/s`;
const clock = (value: number) => new Date(value * 1000).toLocaleTimeString('de-DE', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
function segments(samples: readonly ExitSample[], key: 'rx' | 'tx'): ExitSample[][] {
  const result: ExitSample[][] = [];
  let current: ExitSample[] = [];
  for (const sample of samples) {
    if (sample[key] === null) { current = []; continue; }
    if (!current.length) { current = []; result.push(current); }
    current.push(sample);
  }
  return result;
}
function ExitChart({ name, comment, samples }: { name: string; comment?: string; samples: ExitSample[] }) {
  const last = samples[samples.length - 1];
  const end = last?.timestamp ?? 0;
  const start = samples[0]?.timestamp ?? end;
  const peak = Math.max(125000, ...samples.flatMap(s => [s.rx ?? 0, s.tx ?? 0]));
  const available = samples.some(s => s.rx !== null || s.tx !== null);
  const x = (time: number) => 76 + (time - start) / Math.max(1, end - start) * 272;
  const y = (value: number) => 12 + (1 - value / peak) * 146;
  return <section class="statistics-panel" aria-label={`Ausleitung ${text(name)}`}>
    <h4>{text(name)}</h4>{comment && <p class="exit-comment">{text(comment)}</p>}
    <div class="statistics-values">{(['rx', 'tx'] as const).map((key, index) => <span key={key} class={index ? 'statistics-secondary' : 'statistics-primary'}>{key.toUpperCase()} {last?.[key] == null ? '—' : format(last[key]!)}</span>)}</div>
    <div class="statistics-plot"><svg viewBox="0 0 360 190" role="img" aria-label={`${text(name)}: RX und TX im zeitlichen Verlauf`}>
      {[0, .5, 1].map(f => <g key={f}><line class="statistics-gridline" x1="76" x2="348" y1={12 + f * 146} y2={12 + f * 146} /><text class="statistics-axis" x="70" y={16 + f * 146} text-anchor="end">{format(peak * (1 - f))}</text></g>)}
      {(['rx', 'tx'] as const).map((key, index) => <g key={key} class={index ? 'statistics-secondary' : 'statistics-primary'}>{segments(samples, key).map((part, i) => part.length === 1 ? <circle key={i} cx={x(part[0].timestamp)} cy={y(part[0][key]!)} r="3" /> : <polyline key={i} class="statistics-line" points={part.map(s => `${x(s.timestamp)},${y(s[key]!)}`).join(' ')} />)}</g>)}
      {last && <><text class="statistics-axis" x="76" y="181">{clock(start)}</text><text class="statistics-axis" x="348" y="181" text-anchor="end">{clock(end)}</text></>}
      {!available && <text class="statistics-empty" x="212" y="85" text-anchor="middle">{samples.length ? 'Sammle Messwerte …' : 'Keine Messwerte'}</text>}
    </svg></div>
  </section>;
}
export function ExitStatisticsPanel({ payload, frames, resolution }: { payload: ExitStatusPayload | null; frames: readonly ExitCounterFrame[]; resolution: number | string }) {
  if (!payload?.exits.length) return null;
  return <section class="exit-statistics" aria-label="Statistik der Internet-Ausleitung"><h3>Internet-Ausleitung</h3><div class="statistics-grid">{payload.exits.slice(0, 8).map(exit => <ExitChart key={exit.name} name={exit.name} comment={exit.comment} samples={exitSeries(frames, exit.name, resolution)} />)}</div></section>;
}
