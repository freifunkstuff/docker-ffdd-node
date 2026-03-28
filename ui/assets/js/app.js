const VIEWS = {
  status: document.querySelector('#view-status'),
  nodes: document.querySelector('#view-nodes'),
};

const rows = {
  status: document.querySelector('#status-table'),
  contact: document.querySelector('#contact-table'),
  links: document.querySelector('#links-table'),
  gateways: document.querySelector('#gateways-table'),
};

function safe(value, fallback = '—') {
  if (value === null || value === undefined || value === '') {
    return fallback;
  }
  return String(value);
}

function setView(view) {
  Object.entries(VIEWS).forEach(([name, element]) => {
    element.hidden = name !== view;
    const navLink = document.querySelector(`#nav-${name}`);
    if (navLink) {
      if (name === view) {
        navLink.setAttribute('aria-current', 'page');
      } else {
        navLink.removeAttribute('aria-current');
      }
    }
  });
}

function tableRows(entries) {
  return entries
    .map(([label, value]) => `<tr><th>${label}</th><td>${safe(value)}</td></tr>`)
    .join('');
}

function renderEmptyRow(target, message, colSpan) {
  target.innerHTML = `<tr><td class="empty-row" colspan="${colSpan}">${message}</td></tr>`;
}

function renderData(sysinfo, nodes) {
  const common = sysinfo?.data?.common || {};
  const system = sysinfo?.data?.system || {};
  const contact = sysinfo?.data?.contact || {};
  const statistic = sysinfo?.data?.statistic || {};
  const bmxd = nodes?.bmxd || {};

  document.querySelector('#node-title').textContent = `${safe(contact.name, 'Freifunk Knoten')} (${safe(common.node)})`;

  const now = Date.now();
  const timestamp = Number(nodes?.timestamp || sysinfo?.timestamp || 0) * 1000;
  const ageSeconds = timestamp ? Math.max(0, Math.floor((now - timestamp) / 1000)) : null;
  document.querySelector('#data-age').textContent = ageSeconds === null
    ? 'Zeitstempel unbekannt'
    : `Datenstand vor ${ageSeconds} Sekunden`;

  document.querySelector('#home-summary').textContent =
    `${safe(common.community)} · ${safe(common.domain)} · IP ${safe(common.ip)}`;

  const homeCards = document.querySelector('#home-cards');
  const linksCount = (bmxd.links || []).length;
  const gatewaysCount = (bmxd.gateways?.gateways || []).length;
  const originatorsCount = (bmxd.originators || []).length;
  homeCards.innerHTML = [
    ['Links', linksCount],
    ['Gateways', gatewaysCount],
    ['Originators', originatorsCount],
    ['Uptime', safe(system.uptime_string || system.uptime, '—')],
  ]
    .map(([label, value]) => `<div class="stat-card"><small>${label}</small><strong>${safe(value)}</strong></div>`)
    .join('');

  rows.status.innerHTML = tableRows([
    ['Node-ID', common.node],
    ['IP', common.ip],
    ['Community', common.community],
    ['Domain', common.domain],
    ['Board', system.board],
    ['Model', system.model],
    ['CPU Count', system.cpucount],
    ['Load', statistic.cpu_load],
    ['MemTotal', statistic.meminfo_MemTotal],
    ['MemFree', statistic.meminfo_MemFree],
    ['Fastd RX', statistic.interfaces?.tbb_fastd_rx],
    ['Fastd TX', statistic.interfaces?.tbb_fastd_tx],
  ]);

  rows.contact.innerHTML = tableRows([
    ['Name', contact.name],
    ['E-Mail', contact.email],
    ['Location', contact.location],
    ['Note', contact.note],
  ]);

  const links = bmxd.links || [];
  if (!links.length) {
    renderEmptyRow(rows.links, 'Keine Links vorhanden', 6);
  } else {
    rows.links.innerHTML = links
      .map((link) => `<tr>
        <td>${safe(link.node)}</td>
        <td>${safe(link.ip)}</td>
        <td>${safe(link.interface)}</td>
        <td>${safe(link.rtq)}</td>
        <td>${safe(link.rq)}</td>
        <td>${safe(link.tq)}</td>
      </tr>`)
      .join('');
  }

  const gateways = bmxd.gateways?.gateways || [];
  if (!gateways.length) {
    renderEmptyRow(rows.gateways, 'Keine Gateways vorhanden', 6);
  } else {
    rows.gateways.innerHTML = gateways
      .map((gateway) => `<tr>
        <td>${safe(gateway.node)}</td>
        <td>${safe(gateway.ip)}</td>
        <td>${safe(gateway.best_next_hop)}</td>
        <td>${safe(gateway.brc)}</td>
        <td>${safe(gateway.speed)}</td>
        <td>${safe(gateway.usage)}</td>
      </tr>`)
      .join('');
  }
}

async function loadJson(url) {
  const response = await fetch(url, { cache: 'no-store' });
  if (!response.ok) {
    throw new Error(`HTTP ${response.status} for ${url}`);
  }
  return response.json();
}

async function init() {
  const hash = (window.location.hash || '#status').slice(1);
  setView(VIEWS[hash] ? hash : 'status');

  window.addEventListener('hashchange', () => {
    const current = (window.location.hash || '#status').slice(1);
    setView(VIEWS[current] ? current : 'status');
  });

  try {
    const [sysinfo, nodes] = await Promise.all([
      loadJson('/sysinfo.json'),
      loadJson('/nodes.json'),
    ]);
    renderData(sysinfo, nodes);
  } catch (error) {
    document.querySelector('#home-summary').textContent = `Fehler beim Laden der Daten: ${error.message}`;
  }
}

init();
