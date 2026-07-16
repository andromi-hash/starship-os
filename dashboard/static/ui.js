/* Core UI helpers */
const S = {
  currentView: 'dashboard',
  agents: [],
  agentsMap: {},
  incidents: [],
  dashboard: null,
  fleet: null,
  chatAgent: 'proxy',
  rightPanelContent: false,
  polling: null,
};

async function api(path, opts = {}) {
  try {
    const init = {
      method: opts.method || 'GET',
      headers: { 'Content-Type': 'application/json', ...(opts.headers || {}) },
    };
    if (opts.body !== undefined) {
      init.body = typeof opts.body === 'string' ? opts.body : JSON.stringify(opts.body);
    }
    const res = await fetch(path, init);
    if (!res.ok) {
      const t = await res.text().catch(() => '');
      console.warn('api', path, res.status, t);
      return null;
    }
    return await res.json();
  } catch (e) {
    console.warn('api error', path, e);
    return null;
  }
}

function escapeHtml(s) {
  if (s == null) return '';
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function showToast(msg, ms = 3000, type = 'info') {
  const c = document.getElementById('toast-container');
  if (!c) return;
  const t = document.createElement('div');
  t.className = `toast ${type}`;
  t.textContent = msg;
  c.appendChild(t);
  setTimeout(() => { t.style.opacity = '0'; setTimeout(() => t.remove(), 200); }, ms);
}

function showLoading(area) {
  if (!area) return;
  area.innerHTML = `<div class="empty-state">
    <div class="icon">⟳</div>
    <p>Loading…</p>
  </div>`;
}

function showError(area, msg) {
  if (!area) return;
  area.innerHTML = `<div class="empty-state">
    <div class="icon" style="color:var(--red)">⚠</div>
    <h3>Error</h3>
    <p>${escapeHtml(msg || 'Something went wrong')}</p>
    <button class="btn btn-secondary" style="margin-top:12px" onclick="renderView(S.currentView || 'dashboard')">Retry</button>
  </div>`;
}

function showNoData(area, title, detail) {
  if (!area) return;
  area.innerHTML = `<div class="empty-state">
    <div class="icon">◌</div>
    <h3>${escapeHtml(title || 'No active data')}</h3>
    <p>${escapeHtml(detail || 'This subsystem has no live feed right now.')}</p>
  </div>`;
}

function setRightPanel(html) {
  const el = document.getElementById('right-content');
  if (!el) return;
  el.innerHTML = html;
  S.rightPanelContent = true;
}

function clearRightPanel() {
  const el = document.getElementById('right-content');
  if (!el) return;
  el.innerHTML = `<div class="empty-state" style="padding:24px 8px"><p>Select a crew member or plant for detail.</p></div>`;
  S.rightPanelContent = false;
}

function renderSidebar() {
  const list = document.getElementById('agent-status-list');
  if (!list) return;
  const agents = S.agents || [];
  if (!agents.length) {
    list.innerHTML = `<div class="muted" style="font-size:11px;padding:4px 8px">No crew online</div>`;
    return;
  }
  list.innerHTML = agents.map(a => {
    const st = a.status === 'online' || a.running ? 'green' : 'red';
    return `<div class="agent-status-item" data-agent="${escapeHtml(a.name)}" onclick="selectCrew('${escapeHtml(a.name)}')">
      <span class="status-dot ${st}"></span>
      <span class="agent-name">${escapeHtml(a.name)}</span>
    </div>`;
  }).join('');
}

function renderView(view) {
  S.currentView = view;
  document.querySelectorAll('.nav-item').forEach(el => {
    el.classList.toggle('active', el.dataset.view === view);
  });
  document.querySelectorAll('.view').forEach(el => el.classList.remove('active'));
  const target = document.getElementById(`${view}-content`);
  if (!target) return;
  target.classList.add('active');

  switch (view) {
    case 'dashboard': renderDashboard(target); break;
    case 'agents': renderAgentView(target); break;
    case 'fleet': renderFleetView(target); break;
    case 'chat': renderChatView(target); break;
    case 'incidents': renderIncidentsView(target); break;
    case 'connect': renderConnectView(target); break;
    case 'shield': renderShieldView(target); break;
    case 'policy':
    case 'memory':
    case 'skills':
    case 'telemetry':
    case 'accounts':
    case 'email':
    case 'orgchart':
    case 'goals':
      renderOfflinePanel(target, view);
      break;
    default:
      showNoData(target, view, 'View not implemented');
  }
}

function formatBytes(n) {
  if (n == null || isNaN(n)) return '—';
  const u = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0; let v = Number(n);
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return `${v.toFixed(1)} ${u[i]}`;
}

function pctClass(p) {
  if (p >= 90) return 'bad';
  if (p >= 70) return 'warn';
  return 'good';
}
