import './gateway-status.css';

export type GatewayControlPayload = {
  timestamp?: number;
  role: string;
  healthy?: boolean;
  announcementEnabled?: boolean;
  advertised?: boolean | null;
  checks?: { exit?: boolean; peers?: boolean; admission?: boolean; dns?: boolean };
  gcDryRun?: boolean;
  wg?: { peers?: number; inbound?: number; active?: number; restricted?: boolean; expiryCandidates?: number; retentionDays?: number };
  fastd?: { accepted?: number; connected?: number; restricted?: boolean };
};

const count = (value: unknown): string => typeof value === 'number' && Number.isSafeInteger(value) && value >= 0 ? String(value) : '—';
const object = (value: unknown): Record<string, unknown> => value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {};

/** Read-only public operator status; node-role and malformed payloads remain hidden. */
export function GatewayStatusPanel({ payload }: { payload: GatewayControlPayload | null }) {
  const data = object(payload);
  if (data.role !== 'gateway') return null;
  const wg = object(data.wg), fastd = object(data.fastd), checks = object(data.checks);
  const disabled = data.announcementEnabled === false;
  const announced = !disabled && data.healthy !== false && data.advertised === true;
  const withdrawn = !disabled && data.announcementEnabled === true && (data.advertised === false || data.healthy === false);
  const announcement = disabled ? 'Nicht angekündigt' : announced ? 'Angekündigt' : withdrawn ? 'Zurückgezogen' : 'Unbekannt';
  const announcementClass = disabled ? 'identity-unknown' : announced ? 'connected' : withdrawn ? 'disconnected' : 'identity-unknown';
  const healthClass = data.healthy === true ? 'connected' : data.healthy === false ? 'disconnected' : 'identity-unknown';
  return <section class="panel gateway-status-panel" aria-label="VPN-Gateway">
    <div class="gateway-status-heading"><h3>VPN-Gateway</h3><span class={`status-pill ${healthClass}`}>{data.healthy === true ? 'Bereit' : data.healthy === false ? 'Gestört' : 'Unbekannt'}</span></div>
    <div class="gateway-status-row"><span>Ankündigung</span><span class={`status-pill ${announcementClass}`}>{announcement}</span></div>
    <div class="gateway-status-row"><span>WireGuard</span><span>{count(wg.inbound)} angenommen / {count(wg.active)} verbunden</span></div>
    <div class="gateway-status-row"><span>fastd</span><span>{count(fastd.accepted)} angenommen / {count(fastd.connected)} verbunden</span></div>
    {data.healthy === false && <div class="gateway-status-note">Nicht bereit: {([['exit', 'Internet-Ausleitung'], ['peers', 'Peers'], ['admission', 'Aufnahme'], ['dns', 'DNS']] as const).filter(([key]) => checks[key] !== true).map(([, label]) => label).join(', ')}</div>}
    {(wg.restricted === true || fastd.restricted === true) && <div class="gateway-status-note">Zulassung eingeschränkt: {[wg.restricted === true ? 'WireGuard' : '', fastd.restricted === true ? 'fastd' : ''].filter(Boolean).join(', ')}</div>}
    <div class="gateway-status-note">Schlüsselaufbewahrung {count(wg.retentionDays)} Tage Inaktivität{data.gcDryRun === true ? ' · Testlauf' : ''}{typeof wg.expiryCandidates === 'number' && count(wg.expiryCandidates) !== '—' ? ` · ${count(wg.expiryCandidates)} zur Bereinigung` : ''}</div>
  </section>;
}
