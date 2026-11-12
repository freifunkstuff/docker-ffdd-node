import { exitVolumeDays, formatExitVolume } from './exit-volume-data';
import type { ExitTrafficPayload } from './exit-volume-data';
import './exit-volume.css';

export type { ExitTrafficPayload } from './exit-volume-data';

export function ExitVolumeChart({ payload }: { payload: ExitTrafficPayload | null }) {
  const days = exitVolumeDays(payload);
  const maximum = Math.max(1024 ** 2, ...days.map(({ value }) => value ? value.rx_bytes + value.tx_bytes : 0));
  const unit = maximum >= 1024 ** 3 ? 'GiB' : 'MiB';
  const left = 65, width = 624, top = 12, height = 145, step = width / 30;
  const shortDate = (date: string) => `${date.slice(8, 10)}.${date.slice(5, 7)}.`;
  return <section class="exit-volume-panel" aria-label="Internet-Ausleitung · Datenvolumen">
    <h4>Datenvolumen · letzte 30 Tage</h4>
    <div class="exit-volume-legend"><span class="exit-volume-rx">RX = Download</span><span class="exit-volume-tx statistics-secondary">TX = Upload</span></div>
    <svg class="exit-volume-plot" viewBox="0 0 704 190" role="img" aria-label="Internet-Ausleitung: tägliches Download- und Upload-Datenvolumen der letzten 30 Tage">
      {[0, 0.5, 1].map(fraction => <g key={fraction}>
        <line class="exit-volume-gridline" x1={left} x2={left + width} y1={top + height * fraction} y2={top + height * fraction} />
        <text class="exit-volume-axis" x={left - 7} y={top + height * fraction + 4} text-anchor="end">{formatExitVolume(maximum * (1 - fraction), unit)}</text>
      </g>)}
      {days.map(({ date, value }, index) => {
        const x = left + index * step + 3;
        const rxHeight = value ? value.rx_bytes / maximum * height : 0;
        const txHeight = value ? value.tx_bytes / maximum * height : 0;
        return <g key={date}>
          <title>{`${date}: ${value ? `RX (Download): ${formatExitVolume(value.rx_bytes)} · TX (Upload): ${formatExitVolume(value.tx_bytes)}${value.partial ? ' · Unvollständiger Tag' : ''}` : 'Keine Daten'}`}</title>
          {value ? <>
            <rect class="exit-volume-rx" x={x} y={top + height - rxHeight} width={step - 6} height={rxHeight} />
            <rect class="exit-volume-tx statistics-secondary" x={x} y={top + height - rxHeight - txHeight} width={step - 6} height={txHeight} />
            {rxHeight + txHeight === 0 && <line class="exit-volume-zero" x1={x} x2={x + step - 6} y1={top + height} y2={top + height} />}
            {value.partial && <circle class="exit-volume-partial" cx={x + (step - 6) / 2} cy={Math.max(top + 2, top + height - rxHeight - txHeight - 4)} r="1.7" />}
          </> : <line class="exit-volume-unknown" x1={x} x2={x + step - 6} y1={top + height - 2} y2={top + height - 2} />}
          <rect class="exit-volume-hit" x={left + index * step} y={top} width={step} height={height + 5} />
          {[0, 7, 14, 21, 29].includes(index) && <text class="exit-volume-axis" x={x + (step - 6) / 2} y="180" text-anchor="middle">{shortDate(date)}</text>}
        </g>;
      })}
      {!days.length && <text class="exit-volume-empty" x={left + width / 2} y="88" text-anchor="middle">Keine Daten</text>}
    </svg>
  </section>;
}
