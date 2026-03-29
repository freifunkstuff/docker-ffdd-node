import type { ComponentChild } from 'preact';
import { useEffect, useMemo, useRef, useState } from 'preact/hooks';
import type {
  BackbonePayload,
  BackbonePeer,
  MeshStatusPayload,
  NodesPayload,
  NodeRow,
  SysinfoPayload,
  UiExtensionDefinition,
  UiExtensionRuntimeData,
  UiExtensionsIndexPayload,
} from './types';

type ViewKey = string;
type NavItem = { key: string; label: string; order: number };
type LegalTextState = Record<string, string>;
type ExtensionRuntimeState = Record<string, UiExtensionRuntimeData>;
type ExtensionModule = {
  render: (container: HTMLElement, context: ExtensionRenderContext) => void | Promise<void>;
  dispose?: (container: HTMLElement) => void | Promise<void>;
};
type ExtensionRenderContext = {
  data: Record<string, unknown>;
  error: string;
  updatedAt: number | null;
  nowSeconds: number;
  refreshNow: () => void;
  fetchJson: typeof loadJson;
  fetchText: typeof loadText;
  safe: typeof safe;
  formatAge: (timestamp: string | undefined) => string;
};

const LICENSE_ITEMS = [
  { label: 'Nutzungsbedingungen', href: '/licenses/agreement-de.txt' },
  { label: 'Pico Peering Agreement', href: '/licenses/pico-de.txt' },
  { label: 'GPLv2', href: '/licenses/gpl2.txt' },
  { label: 'GPLv3', href: '/licenses/gpl3.txt' },
];

const CORE_NAV_ITEMS: NavItem[] = [
  { key: 'status', label: 'Status & Kontakt', order: 0 },
  { key: 'nodes', label: 'Nodes', order: 10 },
  { key: 'licenses', label: 'Rechtliches', order: 1000 },
];
const EXTENSIONS_INDEX_URL = '/ui/extensions/index.json';
const CORE_VIEW_KEYS = new Set(CORE_NAV_ITEMS.map((item) => item.key));

function safe(value: unknown, fallback = '—'): string {
  if (value === null || value === undefined || value === '') {
    return fallback;
  }
  return String(value);
}

function currentViewFromHash(): ViewKey {
  const hash = window.location.hash.replace('#', '');
  return hash || 'status';
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

async function loadOptionalJson<T>(url: string): Promise<T | null> {
  const response = await fetch(url, { cache: 'no-store' });
  if (response.status === 404) {
    return null;
  }
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

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function normalizeExtensions(payload: UiExtensionsIndexPayload | null): UiExtensionDefinition[] {
  const seenIds = new Set<string>();
  const seenHashes = new Set<string>();
  const rawEntries = Array.isArray(payload?.extensions) ? payload?.extensions : [];
  const extensions: UiExtensionDefinition[] = [];

  for (const rawEntry of rawEntries) {
    if (!rawEntry || typeof rawEntry !== 'object') {
      continue;
    }

    const id = typeof rawEntry.id === 'string' ? rawEntry.id.trim() : '';
    const label = typeof rawEntry.label === 'string' ? rawEntry.label.trim() : '';
    const hash = typeof rawEntry.hash === 'string' ? rawEntry.hash.trim() : '';
    const entry = typeof rawEntry.entry === 'string' ? rawEntry.entry.trim() : '';
    const order = Number(rawEntry.order);
    const style = typeof rawEntry.style === 'string' && rawEntry.style.trim() ? rawEntry.style.trim() : undefined;
    const endpoints = Array.isArray(rawEntry.endpoints)
      ? rawEntry.endpoints.filter((value): value is string => typeof value === 'string' && value.trim().length > 0)
      : [];

    if (!id || !label || !hash || !entry || !Number.isFinite(order)) {
      continue;
    }

    if (CORE_VIEW_KEYS.has(hash) || seenIds.has(id) || seenHashes.has(hash)) {
      continue;
    }

    seenIds.add(id);
    seenHashes.add(hash);
    extensions.push({ id, label, hash, entry, order, style, endpoints });
  }

  return extensions.sort((left, right) => {
    if (left.order !== right.order) {
      return left.order - right.order;
    }
    return left.label.localeCompare(right.label, 'de');
  });
}

async function loadExtensionData(extensions: UiExtensionDefinition[]): Promise<ExtensionRuntimeState> {
  const result: ExtensionRuntimeState = {};

  await Promise.all(
    extensions.map(async (extension) => {
      if (extension.endpoints.length === 0) {
        result[extension.id] = { data: {}, error: '', updatedAt: null };
        return;
      }

      const settled = await Promise.allSettled(extension.endpoints.map((url) => loadJson<unknown>(url)));
      const data: Record<string, unknown> = {};
      const errors: string[] = [];

      extension.endpoints.forEach((url, index) => {
        const entry = settled[index];
        if (entry.status === 'fulfilled') {
          data[url] = entry.value;
          return;
        }
        errors.push(`${url}: ${errorMessage(entry.reason)}`);
      });

      result[extension.id] = {
        data,
        error: errors.join(' | '),
        updatedAt: Object.keys(data).length > 0 ? Math.floor(Date.now() / 1000) : null,
      };
    }),
  );

  return result;
}

function qualityClass(value: unknown): string {
  const numeric = Number(String(value ?? '').trim());
  if (!Number.isFinite(numeric)) {
    return '';
  }
  if (numeric >= 90) {
    return 'quality-good';
  }
  if (numeric >= 50) {
    return 'quality-medium';
  }
  if (numeric >= 10) {
    return 'quality-low';
  }
  if (numeric >= 0) {
    return 'quality-bad';
  }
  return '';
}

function cellClass(column: keyof NodeRow, value: unknown): string {
  const classes: string[] = [];

  if (column === 'brc') {
    classes.push('col-brc');
  }

  if (column === 'rtq' || column === 'rq' || column === 'tq' || column === 'brc') {
    const quality = qualityClass(value);
    if (quality) {
      classes.push(quality);
    }
  }

  return classes.join(' ');
}

function headerClass(column: keyof NodeRow): string {
  if (column === 'brc') {
    return 'col-brc';
  }
  return '';
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

function boolLabel(value: boolean | undefined, whenTrue: string, whenFalse: string): string {
  return value ? whenTrue : whenFalse;
}

function StatusPanel({ meshStatus }: { meshStatus: MeshStatusPayload | null }) {
  const mesh = meshStatus?.mesh || {};
  const gateway = meshStatus?.gateway || {};
  const meshText = mesh.stable ? 'Connected' : mesh.connected ? 'Stabilizing' : 'Disconnected';
  const meshClass = mesh.stable ? 'connected' : mesh.connected ? 'stale' : 'disconnected';
  const gatewayText = boolLabel(gateway.connected, 'Connected', 'Disconnected');
  const gatewayClass = gateway.connected ? 'connected' : 'disconnected';

  return (
    <section class="panel panel-runtime-status">
      <h3>Status</h3>
      <div class="status-summary">
        <div class="status-summary-row">
          <span>Mesh</span>
          <span class={`status-pill ${meshClass}`}>{meshText}</span>
        </div>
        <div class="status-summary-row">
          <div class="status-summary-label">
            <span>Gateway</span>
            {gateway.selected ? <span class="status-summary-meta">{gateway.selected}</span> : null}
          </div>
          <div class="status-summary-value">
            <span class={`status-pill ${gatewayClass}`}>{gatewayText}</span>
          </div>
        </div>
      </div>
    </section>
  );
}

function BackboneTable({ peers }: { peers: BackbonePeer[] }) {
  return (
    <section class="panel panel-backbones">
      <div class="panel-head">
        <h3>Backbones</h3>
        <span class="muted">{peers.filter((peer) => peer.status === 'connected').length} / {peers.length}</span>
      </div>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Typ</th>
              <th>Hostname (Port)</th>
              <th>Interface</th>
              <th>Status</th>
            </tr>
          </thead>
          <tbody>
            {peers.length === 0 ? (
              <tr>
                <td colSpan={4} class="muted">Keine Backbone-Peers konfiguriert</td>
              </tr>
            ) : (
              peers.map((peer, index) => (
                <tr key={`${peer.type || 'peer'}-${peer.host || 'host'}-${peer.port || 'port'}-${index}`}>
                  <td>{safe(peer.type)}</td>
                  <td>{safe(peer.host, '')}:{safe(peer.port, '')}</td>
                  <td>{safe(peer.interface)}</td>
                  <td>
                    <span class={`status-pill ${safe(peer.status, '').toLowerCase()}`}>{safe(peer.status)}</span>
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function NodeTable({
  title,
  columns,
  rows,
  filter,
  emptyText,
  rowClassName,
  renderCell,
}: {
  title: string;
  columns: Array<{ key: keyof NodeRow; label: string }>;
  rows: NodeRow[];
  filter: string;
  emptyText: string;
  rowClassName?: (row: NodeRow) => string;
  renderCell?: (row: NodeRow, column: keyof NodeRow) => ComponentChild;
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
                <th key={String(column.key)} class={headerClass(column.key)}>{column.label}</th>
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
                <tr key={`${row.ip || row.node || 'row'}-${index}`} class={rowClassName ? rowClassName(row) : ''}>
                  {columns.map((column) => (
                    <td key={String(column.key)} class={cellClass(column.key, row[column.key])}>
                      {renderCell ? renderCell(row, column.key) : safe(row[column.key])}
                    </td>
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

function ExtensionView({
  extension,
  runtimeData,
  nowSeconds,
}: {
  extension: UiExtensionDefinition;
  runtimeData: UiExtensionRuntimeData;
  nowSeconds: number;
}) {
  const hostRef = useRef<HTMLDivElement>(null);
  const moduleRef = useRef<ExtensionModule | null>(null);
  const disposeRef = useRef<(() => void | Promise<void>) | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [loadError, setLoadError] = useState<string>('');

  const context = useMemo<ExtensionRenderContext>(() => ({
    data: runtimeData.data,
    error: runtimeData.error,
    updatedAt: runtimeData.updatedAt,
    nowSeconds,
    refreshNow: () => undefined,
    fetchJson: loadJson,
    fetchText: loadText,
    safe,
    formatAge: (timestamp: string | undefined) => formatAge(timestamp, nowSeconds),
  }), [nowSeconds, runtimeData.data, runtimeData.error, runtimeData.updatedAt]);

  useEffect(() => {
    let canceled = false;

    async function loadModule() {
      setLoading(true);
      setLoadError('');
      moduleRef.current = null;

      if (disposeRef.current && hostRef.current) {
        await Promise.resolve(disposeRef.current());
        disposeRef.current = null;
      }

      if (hostRef.current) {
        hostRef.current.textContent = '';
      }

      try {
        if (extension.style && !document.querySelector(`link[data-extension-style="${extension.id}"]`)) {
          const link = document.createElement('link');
          link.rel = 'stylesheet';
          link.href = extension.style;
          link.dataset.extensionStyle = extension.id;
          document.head.appendChild(link);
        }

        const loaded = await import(/* @vite-ignore */ extension.entry) as ExtensionModule;
        if (canceled) {
          return;
        }
        if (typeof loaded.render !== 'function') {
          throw new Error(`Extension ${extension.id} exportiert keine render()-Funktion`);
        }
        moduleRef.current = loaded;
        disposeRef.current = loaded.dispose ? () => loaded.dispose!(hostRef.current!) : null;
        if (hostRef.current) {
          await Promise.resolve(loaded.render(hostRef.current, context));
        }
        setLoading(false);
      } catch (error) {
        if (canceled) {
          return;
        }
        setLoadError(errorMessage(error));
        setLoading(false);
      }
    }

    void loadModule();

    return () => {
      canceled = true;
      const currentDispose = disposeRef.current;
      const currentHost = hostRef.current;
      disposeRef.current = null;
      moduleRef.current = null;
      if (currentDispose && currentHost) {
        void Promise.resolve(currentDispose());
      }
      if (currentHost) {
        currentHost.textContent = '';
      }
    };
  }, [extension.entry, extension.id, extension.style]);

  useEffect(() => {
    let canceled = false;

    async function renderModule() {
      if (!moduleRef.current || !hostRef.current) {
        return;
      }
      try {
        await Promise.resolve(moduleRef.current.render(hostRef.current, context));
        if (!canceled) {
          setLoadError('');
        }
      } catch (error) {
        if (!canceled) {
          setLoadError(errorMessage(error));
        }
      }
    }

    void renderModule();

    return () => {
      canceled = true;
    };
  }, [context, extension.id]);

  return (
    <section class="panel extension-panel">
      <h3>{extension.label}</h3>
      {loading ? <p class="muted">Erweiterung wird geladen …</p> : null}
      {loadError ? <div class="error-box">Fehler beim Laden der Erweiterung: {loadError}</div> : null}
      {!loadError ? <div ref={hostRef} class="extension-host" /> : null}
    </section>
  );
}

export function App() {
  const [view, setView] = useState<ViewKey>(currentViewFromHash());
  const [sysinfo, setSysinfo] = useState<SysinfoPayload | null>(null);
  const [nodes, setNodes] = useState<NodesPayload | null>(null);
  const [backbone, setBackbone] = useState<BackbonePayload | null>(null);
  const [meshStatus, setMeshStatus] = useState<MeshStatusPayload | null>(null);
  const [error, setError] = useState<string>('');
  const [extensions, setExtensions] = useState<UiExtensionDefinition[]>([]);
  const [extensionsLoaded, setExtensionsLoaded] = useState<boolean>(false);
  const [extensionData, setExtensionData] = useState<ExtensionRuntimeState>({});
  const [nodeFilter, setNodeFilter] = useState<string>('');
  const [legalTexts, setLegalTexts] = useState<LegalTextState>({});
  const [nowSeconds, setNowSeconds] = useState<number>(Math.floor(Date.now() / 1000));
  const [lastRefreshAttempt, setLastRefreshAttempt] = useState<number>(0);
  const [retryAfter, setRetryAfter] = useState<number>(0);
  const refreshInFlightRef = useRef<boolean>(false);

  const extensionSignature = useMemo(
    () => JSON.stringify(extensions.map((extension) => [extension.id, extension.hash, extension.entry, extension.style || '', extension.endpoints.join('|')])),
    [extensions],
  );

  async function refreshData(extensionsSnapshot: UiExtensionDefinition[], isCanceled?: () => boolean): Promise<void> {
    if (refreshInFlightRef.current) {
      return;
    }

    refreshInFlightRef.current = true;
    const attemptAt = Math.floor(Date.now() / 1000);
    setLastRefreshAttempt(attemptAt);

    try {
      const [sysinfoData, nodesData, backboneData, meshStatusData, extensionRuntime] = await Promise.all([
        loadJson<SysinfoPayload>('/sysinfo.json'),
        loadJson<NodesPayload>('/nodes.json'),
        loadJson<BackbonePayload>('/backbone.json'),
        loadJson<MeshStatusPayload>('/mesh-status.json'),
        loadExtensionData(extensionsSnapshot),
      ]);

      if (isCanceled?.()) {
        return;
      }

      setSysinfo(sysinfoData);
      setNodes(nodesData);
      setBackbone(backboneData);
      setMeshStatus(meshStatusData);
      setExtensionData(extensionRuntime);
      setError('');
      setRetryAfter(0);
    } catch (err) {
      if (isCanceled?.()) {
        return;
      }
      setError(errorMessage(err));
      setRetryAfter(attemptAt + 30);
    } finally {
      refreshInFlightRef.current = false;
    }
  }

  useEffect(() => {
    const onHashChange = () => setView(currentViewFromHash());
    window.addEventListener('hashchange', onHashChange);
    return () => window.removeEventListener('hashchange', onHashChange);
  }, []);

  useEffect(() => {
    let canceled = false;

    async function loadRegistry() {
      try {
        const payload = await loadOptionalJson<UiExtensionsIndexPayload>(EXTENSIONS_INDEX_URL);
        if (canceled) {
          return;
        }
        setExtensions(normalizeExtensions(payload));
      } catch (err) {
        if (!canceled) {
          console.error('UI extensions could not be loaded', err);
          setExtensions([]);
        }
      } finally {
        if (!canceled) {
          setExtensionsLoaded(true);
        }
      }
    }

    void loadRegistry();

    return () => {
      canceled = true;
    };
  }, []);

  useEffect(() => {
    let canceled = false;
    void refreshData(extensions, () => canceled);
    return () => {
      canceled = true;
    };
  }, [extensionSignature]);

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

      const runtimeTimestamps = [
        Number(nodes?.timestamp || 0),
        Number(sysinfo?.timestamp || 0),
        Number(backbone?.timestamp || 0),
        meshStatus?.updated_at ? Math.floor(new Date(meshStatus.updated_at).getTime() / 1000) : 0,
        ...Object.values(extensionData).map((entry) => entry.updatedAt || 0),
      ].filter((value) => Number.isFinite(value) && value > 0);

      const timestamp = runtimeTimestamps.reduce((acc, value) => (value > acc ? value : acc), 0);
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
      await refreshData(extensions);
    }

    void refreshIfNeeded();
  }, [backbone?.timestamp, error, extensionData, extensions, lastRefreshAttempt, meshStatus?.updated_at, nodes?.timestamp, nowSeconds, retryAfter, sysinfo?.timestamp]);

  useEffect(() => {
    if (!extensionsLoaded) {
      return;
    }

    const validViews = new Set<string>([...CORE_NAV_ITEMS.map((item) => item.key), ...extensions.map((extension) => extension.hash)]);
    if (validViews.has(view)) {
      return;
    }

    setView('status');
    if (window.location.hash !== '#status') {
      window.location.hash = '#status';
    }
  }, [extensions, extensionsLoaded, view]);

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
  const backbonePeers = backbone?.peers || [];
  const connectedBackbones = backbonePeers.filter((peer) => peer.status === 'connected').length;
  const extensionNavItems = useMemo<NavItem[]>(
    () => extensions.map((extension) => ({ key: extension.hash, label: extension.label, order: extension.order })),
    [extensions],
  );
  const navItems = useMemo(
    () => [...CORE_NAV_ITEMS, ...extensionNavItems].sort((left, right) => left.order - right.order),
    [extensionNavItems],
  );
  const activeExtension = extensions.find((extension) => extension.hash === view) || null;

  const communityName = safe(common.community);
  const communityLink = common.domain ? `https://${common.domain}` : 'https://freifunk.net';
  const titleName = safe(contact.name, 'Freifunk Knoten');
  const titleNode = safe(common.node, '?');
  const titleCommunity = safe(common.community, '?');
  const currentTimestamp = (() => {
    const runtimeTimestamps = [sysinfo?.timestamp, nodes?.timestamp, backbone?.timestamp]
      .map((value) => Number(value || 0))
      .filter((value) => Number.isFinite(value) && value > 0);
    const meshTimestamp = meshStatus?.updated_at ? Math.floor(new Date(meshStatus.updated_at).getTime() / 1000) : 0;
    const extensionTimestamps = Object.values(extensionData)
      .map((entry) => entry.updatedAt || 0)
      .filter((value) => value > 0);
    const latest = [...runtimeTimestamps, meshTimestamp, ...extensionTimestamps].reduce((acc, value) => (value > acc ? value : acc), 0);
    return String(latest);
  })();

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
        <a class="topbar-community" href={communityLink} target="_blank" rel="noreferrer">
          Freifunk {communityName}
        </a>
        <div class="topbar-meta">
          <span>IP: {safe(common.ip)}</span>
          <span>Uptime: {safe(system.uptime_string || system.uptime)}</span>
          <span class={`data-age ${ageClass(currentTimestamp, nowSeconds)}`}>{formatAge(currentTimestamp, nowSeconds)}</span>
        </div>
      </header>

      <div class="layout-body">
        <aside class="sidebar">
          <nav>
            {navItems.map((item) => (
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
                  <h3>Backbones</h3>
                  <strong>{connectedBackbones} / {backbonePeers.length}</strong>
                </article>
                <article class="card">
                  <h3>Links</h3>
                  <strong>{(bmxd.links || []).length}</strong>
                </article>
                <article class="card">
                  <h3>Gateways</h3>
                  <strong>{(bmxd.gateways?.gateways || []).length}</strong>
                </article>
                <article class="card">
                  <h3>Freifunk-Knoten</h3>
                  <strong>{(bmxd.originators || []).length}</strong>
                </article>
              </section>

              <section class="status-layout">
                <StatusPanel meshStatus={meshStatus} />

                <section class="panel panel-status">
                  <h3>Info</h3>
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

                <section class="panel panel-contact">
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

                <BackboneTable peers={backbonePeers} />
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
                rowClassName={(row) => (row.ip === bmxd.gateways?.selected ? 'row-active' : '')}
                renderCell={(row, column) => {
                  if (column === 'ip' && row.ip === bmxd.gateways?.selected) {
                    return `${safe(row.ip)} (aktiv)`;
                  }
                  return safe(row[column]);
                }}
                columns={[
                  { key: 'node', label: 'Node' },
                  { key: 'ip', label: 'IP' },
                  { key: 'best_next_hop', label: 'Best Next Hop' },
                  { key: 'speed', label: 'Speed' },
                  { key: 'usage', label: 'Usage' },
                  { key: 'brc', label: 'BRC' },
                ]}
              />

              <NodeTable
                title="Freifunk-Knoten"
                rows={bmxd.originators || []}
                filter={nodeFilter}
                emptyText="Keine passenden Freifunk-Knoten"
                columns={[
                  { key: 'node', label: 'Node' },
                  { key: 'ip', label: 'IP' },
                  { key: 'interface', label: 'Interface' },
                  { key: 'best_next_hop', label: 'Best Next Hop' },
                  { key: 'brc', label: 'BRC' },
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

          {activeExtension ? (
            <ExtensionView
              extension={activeExtension}
              runtimeData={extensionData[activeExtension.id] || { data: {}, error: '', updatedAt: null }}
              nowSeconds={nowSeconds}
            />
          ) : null}

          {!activeExtension && view !== 'status' && view !== 'nodes' && view !== 'licenses' ? (
            <section class="panel">
              <h3>Unbekannter View</h3>
              <p class="muted">Der angeforderte UI-Bereich ist nicht registriert.</p>
            </section>
          ) : null}
        </main>
      </div>
    </div>
  );
}
