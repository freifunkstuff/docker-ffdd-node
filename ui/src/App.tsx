import { useEffect, useMemo, useRef, useState } from 'preact/hooks';
import type { NodesPayload, NodeRow, SysinfoPayload } from './types';

type ViewKey = 'status' | 'nodes' | 'licenses';

const LICENSE_ITEMS = [
  { label: 'Nutzungsbedingungen', href: '/licenses/agreement-de.txt' },
  { label: 'Pico Peering Agreement', href: '/licenses/pico-de.txt' },
  { label: 'GPLv2', href: '/licenses/gpl2.txt' },
  { label: 'GPLv3', href: '/licenses/gpl3.txt' },
];

const NAV_ITEMS: Array<{ key: ViewKey; label: string }> = [
  { key: 'status', label: 'Status & Kontakt' },
  { key: 'nodes', label: 'Nodes' },
  { key: 'licenses', label: 'Rechtliches' },
];

type LegalTextState = Record<string, string>;

function safe(value: unknown, fallback = '—'): string {
  if (value === null || value === undefined || value === '') {
    return fallback;
  }
  return String(value);
}

function currentViewFromHash(): ViewKey {
  const hash = window.location.hash.replace('#', '');
  if (hash === 'nodes' || hash === 'licenses' || hash === 'status') {
    return hash;
  }
  return 'status';
}

function matchesFilter(row: NodeRow, query: string): boolean {
  if (!query) {
    return true;
  }
  const haystack = [
    row.node,
    row.ip,
    row.interface,
    row.rtq,
    row.rq,
    row.tq,
    row.best_next_hop,
    row.brc,
    row.speed,
    row.usage,
    row.type,
  ]
    .map((v) => safe(v, '').toLowerCase())
    .join(' ');
  return haystack.includes(query);
}

function formatAge(timestamp: string | undefined, nowSeconds: number): string {
  const ts = Number(timestamp || 0);
  if (!ts) {
    return 'Zeitstempel unbekannt';
  }
  const age = Math.max(0, Math.floor(nowSeconds - ts));
  return `Datenstand vor ${age} Sekunden`;
}

function ageClass(timestamp: string | undefined, nowSeconds: number): string {
  const ts = Number(timestamp || 0);
  if (!ts) {
    return 'stale';
  }
  const age = Math.max(0, Math.floor(nowSeconds - ts));
  return age <= 75 ? 'fresh' : 'stale';
}

async function loadJson<T>(url: string): Promise<T> {
  const response = await fetch(url, { cache: 'no-store' });
  if (!response.ok) {
    throw new Error(`HTTP ${response.status} für ${url}`);
  }
  return response.json() as Promise<T>;
}

async function loadText(url: string): Promise<string> {
  const response = await fetch(url, { cache: 'no-store' });
  if (!response.ok) {
    throw new Error(`HTTP ${response.status} für ${url}`);
  }
  return response.text();
}

function decodeHtmlEntities(text: string): string {
  const textarea = document.createElement('textarea');
  textarea.innerHTML = text;
  return textarea.value;
}

function KeyValueTable({ rows }: { rows: Array<[string, unknown]> }) {
  return (
    <table class="kv-table">
      <tbody>
        {rows.map(([label, value]) => (
          <tr key={label}>
            <th>{label}</th>
            <td>{safe(value)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function NodeTable({
  title,
  columns,
  rows,
  filter,
  emptyText,
}: {
  title: string;
  columns: Array<{ key: keyof NodeRow; label: string }>;
  rows: NodeRow[];
  filter: string;
  emptyText: string;
}) {
  const filtered = useMemo(
    () => rows.filter((row) => matchesFilter(row, filter.trim().toLowerCase())),
    [rows, filter],
  );

  return (
    <section class="panel">
      <div class="panel-head">
        <h3>{title}</h3>
        <span class="muted">{filtered.length} / {rows.length}</span>
      </div>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              {columns.map((column) => (
                <th key={String(column.key)}>{column.label}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {filtered.length === 0 ? (
              <tr>
                <td colSpan={columns.length} class="muted">{emptyText}</td>
              </tr>
            ) : (
              filtered.map((row, index) => (
                <tr key={`${row.ip || row.node || 'row'}-${index}`}>
                  {columns.map((column) => (
                    <td key={String(column.key)}>{safe(row[column.key])}</td>
                  ))}
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </section>
  );
}

export function App() {
  const [view, setView] = useState<ViewKey>(currentViewFromHash());
  const [sysinfo, setSysinfo] = useState<SysinfoPayload | null>(null);
  const [nodes, setNodes] = useState<NodesPayload | null>(null);
  const [error, setError] = useState<string>('');
  const [nodeFilter, setNodeFilter] = useState<string>('');
  const [legalTexts, setLegalTexts] = useState<LegalTextState>({});
  const [nowSeconds, setNowSeconds] = useState<number>(Math.floor(Date.now() / 1000));
  const [lastRefreshAttempt, setLastRefreshAttempt] = useState<number>(0);
  const [retryAfter, setRetryAfter] = useState<number>(0);
  const refreshInFlightRef = useRef<boolean>(false);

  useEffect(() => {
    const onHashChange = () => setView(currentViewFromHash());
    window.addEventListener('hashchange', onHashChange);
    return () => window.removeEventListener('hashchange', onHashChange);
  }, []);

  useEffect(() => {
    let canceled = false;

    async function refresh() {
      if (refreshInFlightRef.current) {
        return;
      }
      refreshInFlightRef.current = true;

      const attemptAt = Math.floor(Date.now() / 1000);
      setLastRefreshAttempt(attemptAt);
      try {
        const [sysinfoData, nodesData] = await Promise.all([
          loadJson<SysinfoPayload>('/sysinfo.json'),
          loadJson<NodesPayload>('/nodes.json'),
        ]);
        if (canceled) {
          return;
        }
        setSysinfo(sysinfoData);
        setNodes(nodesData);
        setError('');
        setRetryAfter(0);
      } catch (err) {
        if (canceled) {
          return;
        }
        setError((err as Error).message);
        setRetryAfter(attemptAt + 30);
      } finally {
        refreshInFlightRef.current = false;
      }
    }

    refresh();

    return () => {
      canceled = true;
    };
  }, []);

  useEffect(() => {
    const intervalId = window.setInterval(() => {
      setNowSeconds(Math.floor(Date.now() / 1000));
    }, 1000);
    return () => {
      window.clearInterval(intervalId);
    };
  }, []);

  useEffect(() => {
    async function refreshIfNeeded() {
      if (refreshInFlightRef.current) {
        return;
      }

      const timestampRaw = nodes?.timestamp || sysinfo?.timestamp || '0';
      const timestamp = Number(timestampRaw);
      const dataAge = timestamp > 0 ? nowSeconds - timestamp : Number.POSITIVE_INFINITY;
      const shouldRetryFailed = !!error && retryAfter > 0 && nowSeconds >= retryAfter;
      const shouldRefreshStale = !error && dataAge > 30;

      if (!shouldRetryFailed && !shouldRefreshStale) {
        return;
      }

      if (lastRefreshAttempt > 0 && nowSeconds - lastRefreshAttempt < 1) {
        return;
      }

      const attemptAt = Math.floor(Date.now() / 1000);
      setLastRefreshAttempt(attemptAt);
      refreshInFlightRef.current = true;

      try {
        const [sysinfoData, nodesData] = await Promise.all([
          loadJson<SysinfoPayload>('/sysinfo.json'),
          loadJson<NodesPayload>('/nodes.json'),
        ]);
        setSysinfo(sysinfoData);
        setNodes(nodesData);
        setError('');
        setRetryAfter(0);
      } catch (err) {
        setError((err as Error).message);
        setRetryAfter(attemptAt + 30);
      } finally {
        refreshInFlightRef.current = false;
      }
    }

    refreshIfNeeded();
  }, [error, lastRefreshAttempt, nowSeconds, nodes?.timestamp, retryAfter, sysinfo?.timestamp]);


  useEffect(() => {
    let canceled = false;

    async function fetchLegalTexts() {
      const loaded = await Promise.all(
        LICENSE_ITEMS.map(async (item) => {
          try {
            const text = await loadText(item.href);
            return [item.href, decodeHtmlEntities(text)] as const;
          } catch {
            return [item.href, 'Dokument konnte nicht geladen werden.'] as const;
          }
        }),
      );

      if (canceled) {
        return;
      }

      setLegalTexts(Object.fromEntries(loaded));
    }

    fetchLegalTexts();

    return () => {
      canceled = true;
    };
  }, []);

  const common = sysinfo?.data?.common || {};
  const system = sysinfo?.data?.system || {};
  const statistic = sysinfo?.data?.statistic || {};
  const contact = sysinfo?.data?.contact || {};
  const bmxd = nodes?.bmxd || {};

  const communityName = safe(common.community);
  const communityLink = common.domain ? `https://${common.domain}` : 'https://freifunk.net';
  const titleName = safe(contact.name, 'Freifunk Knoten');
  const titleNode = safe(common.node, '?');
  const titleCommunity = safe(common.community, '?');
  const currentTimestamp = nodes?.timestamp || sysinfo?.timestamp;

  useEffect(() => {
    document.title = `${titleNode} ${titleName} - Freifunk ${titleCommunity}`;
  }, [titleName, titleNode, titleCommunity]);

  return (
    <div class="layout">
      <header class="topbar">
        <div class="topbar-title">
          <h1>{titleName}</h1>
          <p class="muted">Knoten {safe(common.node)}</p>
        </div>
        <div class="topbar-meta">
          <a href={communityLink} target="_blank" rel="noreferrer">Freifunk {communityName}</a>
          <span>IP: {safe(common.ip)}</span>
          <span>Uptime: {safe(system.uptime_string || system.uptime)}</span>
          <span class={`data-age ${ageClass(currentTimestamp, nowSeconds)}`}>{formatAge(currentTimestamp, nowSeconds)}</span>
        </div>
      </header>

      <div class="layout-body">
        <aside class="sidebar">
          <nav>
            {NAV_ITEMS.map((item) => (
              <a
                key={item.key}
                href={`#${item.key}`}
                class={view === item.key ? 'active' : ''}
                onClick={() => setView(item.key)}
              >
                {item.label}
              </a>
            ))}
          </nav>
        </aside>

        <main class="content">
          {error ? <div class="error-box">Fehler beim Laden: {error}</div> : null}

          {view === 'status' ? (
            <>
              <section class="cards">
                <article class="card">
                  <h3>Links</h3>
                  <strong>{(bmxd.links || []).length}</strong>
                </article>
                <article class="card">
                  <h3>Gateways</h3>
                  <strong>{(bmxd.gateways?.gateways || []).length}</strong>
                </article>
                <article class="card">
                  <h3>Originators</h3>
                  <strong>{(bmxd.originators || []).length}</strong>
                </article>
                <article class="card">
                  <h3>Community</h3>
                  <strong>{communityName}</strong>
                </article>
              </section>

              <section class="grid-2">
                <section class="panel">
                  <h3>Status</h3>
                  <KeyValueTable
                    rows={[
                      ['Node-ID', common.node],
                      ['IP', common.ip],
                      ['Domain', common.domain],
                      ['Board', system.board],
                      ['Model', system.model],
                      ['CPU Count', system.cpucount],
                      ['Load', statistic.cpu_load],
                      ['MemTotal', statistic.meminfo_MemTotal],
                      ['MemFree', statistic.meminfo_MemFree],
                      ['Fastd RX', statistic.interfaces?.tbb_fastd_rx],
                      ['Fastd TX', statistic.interfaces?.tbb_fastd_tx],
                    ]}
                  />
                </section>

                <section class="panel">
                  <h3>Kontakt</h3>
                  <KeyValueTable
                    rows={[
                      ['Name', contact.name],
                      ['E-Mail', contact.email],
                      ['Ort', contact.location],
                      ['Notiz', contact.note],
                    ]}
                  />
                </section>
              </section>
            </>
          ) : null}

          {view === 'nodes' ? (
            <>
              <section class="panel">
                <div class="panel-head">
                  <h3>Filter</h3>
                </div>
                <input
                  type="search"
                  placeholder="Suche nach Node, IP, Interface, BRC, Speed …"
                  value={nodeFilter}
                  onInput={(event) => setNodeFilter((event.target as HTMLInputElement).value)}
                />
              </section>

              <NodeTable
                title="Links"
                rows={bmxd.links || []}
                filter={nodeFilter}
                emptyText="Keine passenden Links"
                columns={[
                  { key: 'node', label: 'Node' },
                  { key: 'ip', label: 'IP' },
                  { key: 'interface', label: 'Interface' },
                  { key: 'rtq', label: 'RTQ' },
                  { key: 'rq', label: 'RQ' },
                  { key: 'tq', label: 'TQ' },
                ]}
              />

              <NodeTable
                title="Gateways"
                rows={bmxd.gateways?.gateways || []}
                filter={nodeFilter}
                emptyText="Keine passenden Gateways"
                columns={[
                  { key: 'node', label: 'Node' },
                  { key: 'ip', label: 'IP' },
                  { key: 'best_next_hop', label: 'Best Next Hop' },
                  { key: 'brc', label: 'BRC' },
                  { key: 'speed', label: 'Speed' },
                  { key: 'usage', label: 'Usage' },
                ]}
              />

              <NodeTable
                title="Originators"
                rows={bmxd.originators || []}
                filter={nodeFilter}
                emptyText="Keine passenden Originators"
                columns={[
                  { key: 'node', label: 'Node' },
                  { key: 'ip', label: 'IP' },
                  { key: 'interface', label: 'Interface' },
                  { key: 'best_next_hop', label: 'Best Next Hop' },
                  { key: 'brc', label: 'BRC' },
                  { key: 'type', label: 'Type' },
                ]}
              />
            </>
          ) : null}

          {view === 'licenses' ? (
            <section class="panel">
              <h3>Rechtliches</h3>
              <div class="legal-grid">
                {LICENSE_ITEMS.map((item) => (
                  <article class="legal-card" key={item.href}>
                    <div class="panel-head">
                      <h3>{item.label}</h3>
                      <a href={item.href} target="_blank" rel="noreferrer">Öffnen</a>
                    </div>
                    <pre class="legal-text">{legalTexts[item.href] || 'Lade Dokument …'}</pre>
                  </article>
                ))}
              </div>
            </section>
          ) : null}
        </main>
      </div>
    </div>
  );
}
