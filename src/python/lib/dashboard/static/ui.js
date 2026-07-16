/* ── Agnetic Dashboard UI Core ─────────────────────────────────────────── */

const S = {
  currentView: 'dashboard',
  session: null,
  agents: [],
  incidents: [],
  memory: [],
  chatSessions: [],
  currentChatSession: null,
  rightPanelContent: null,
  polling: null,
};

function escapeHtml(str) {
  if (!str) return '';
  const d = document.createElement('div');
  d.textContent = str;
  return d.innerHTML;
}

async function api(path, opts = {}) {
  const headers = { ...opts.headers };
  const password = localStorage.getItem('agnetic_password');
  if (password) headers['Authorization'] = `Bearer ${password}`;
  if (opts.body && typeof opts.body === 'object' && !(opts.body instanceof FormData)) {
    headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(opts.body);
  }
  try {
    const res = await fetch(path, { ...opts, headers });
    if (res.status === 401) {
      const pw = prompt('Dashboard password:');
      if (pw) {
        localStorage.setItem('agnetic_password', pw);
        headers['Authorization'] = `Bearer ${pw}`;
        const retry = await fetch(path, { ...opts, headers });
        if (retry.ok) return retry.json();
        showToast('Authentication failed', 3000, 'error');
        return null;
      }
      showToast('Authentication required', 3000, 'error');
      return null;
    }
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      showToast(err.error || `Request failed: ${res.status}`, 3000, 'error');
      return null;
    }
    const ct = res.headers.get('content-type') || '';
    if (ct.includes('application/json')) return res.json();
    return res;
  } catch (e) {
    showToast(`Network error: ${e.message}`, 3000, 'error');
    return null;
  }
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
  area.innerHTML = `<div class="empty-state" style="padding:40px;text-align:center;color:rgba(255,255,255,0.5)">
    <div style="font-size:24px;margin-bottom:8px">&#9696;</div>
    <p>Loading…</p>
  </div>`;
}

function showError(area, msg) {
  if (!area) return;
  area.innerHTML = `<div class="empty-state" style="padding:40px;text-align:center">
    <div style="font-size:24px;margin-bottom:8px;color:#ff5252">&#9888;</div>
    <h3 style="color:#fff;margin-bottom:6px">Error</h3>
    <p style="color:rgba(255,255,255,0.5)">${escapeHtml(msg || 'Something went wrong')}</p>
    <button class="btn btn-secondary" style="margin-top:12px" onclick="renderView(S.currentView || 'dashboard')">Retry</button>
  </div>`;
}

function showConfirmDialog(msg) {
  return new Promise((resolve) => {
    const overlay = document.getElementById('modal-overlay');
    document.getElementById('modal-message').textContent = msg;
    overlay.style.display = 'flex';
    const confirm = document.getElementById('modal-confirm');
    const cancel = document.getElementById('modal-cancel');
    const cleanup = () => {
      overlay.style.display = 'none';
      confirm.onclick = null;
      cancel.onclick = null;
    };
    confirm.onclick = () => { cleanup(); resolve(true); };
    cancel.onclick = () => { cleanup(); resolve(false); };
  });
}

function renderSidebar() {
  const list = document.getElementById('agent-status-list');
  if (!list) return;
  list.innerHTML = S.agents.map(a =>
    `<div class="agent-status-item" data-agent="${escapeHtml(a.name)}">
      <span class="status-dot ${a.status === 'online' ? 'green' : a.status === 'busy' ? 'yellow' : 'red'}"></span>
      <span class="agent-name">${escapeHtml(a.name)}</span>
    </div>`
  ).join('');
  list.querySelectorAll('.agent-status-item').forEach(el => {
    el.addEventListener('click', () => {
      renderView('agents', { agent: el.dataset.agent });
    });
  });
  const agentBadge = document.getElementById('agent-badge');
  if (agentBadge) agentBadge.textContent = S.agents.length;
  const openIncidents = S.incidents.filter(i => i.status !== 'resolved').length;
  const incBadge = document.getElementById('incident-badge');
  if (incBadge) incBadge.textContent = openIncidents;
}

function renderView(view, data) {
  S.currentView = view;
  document.querySelectorAll('.nav-item').forEach(n => n.classList.toggle('active', n.dataset.view === view));

  const titles = {
    dashboard: ['Overview', 'Your agent mesh at a glance'],
    agents: ['Agents', 'Manage your agent fleet'],
    chat: ['Chat', 'Talk to any agent'],
    incidents: ['Incidents', 'Active issues and runbooks'],
    policy: ['Policy', 'Governance and access control'],
    memory: ['Memory', 'All 7 memory types'],
    shield: ['Droid Shield', 'Secret detection and git guardrails'],
    skills: ['Skills', 'Installed agent capabilities'],
    telemetry: ['Telemetry', 'System observability and events'],
    accounts: ['Service Accounts', 'API keys and permissions'],
    email: ['Agent Email', 'SMTP and Mailchain messaging'],
    orgchart: ['Org Chart', 'Agent hierarchy and goal alignment'],
    goals: ['Goals', 'Objective tracking and milestone management'],
  };
  const [title, subtitle] = titles[view] || [view, ''];
  document.getElementById('view-title').textContent = title;
  document.getElementById('view-subtitle').textContent = subtitle;

  clearRightPanel();

  // Hide all content divs, show the relevant one
  document.querySelectorAll('#view-content > div').forEach(d => d.style.display = 'none');
  const targetId = view + '-content';
  const target = document.getElementById(targetId);
  if (!target) return;
  target.style.display = 'block';

  switch (view) {
    case 'dashboard': renderDashboard(target); break;
    case 'agents': renderAgentView(target, data); break;
    case 'chat': renderChatView(target, data); break;
    case 'policy': renderPolicyView(target); break;
    case 'memory': renderMemoryView(target); break;
    case 'skills': renderSkillsView(target); break;
    case 'incidents': renderIncidentsView(target); break;
    case 'shield': renderShieldView(target); break;
    case 'telemetry': renderTelemetryView(target); break;
    case 'accounts': renderAccountsView(target); break;
    case 'email': renderEmailView(target); break;
    case 'orgchart': renderOrgChartView(target); break;
    case 'goals': renderOrgChartView(target); break;
    default:
      target.innerHTML = `<div class="empty-state"><div class="icon">&#9881;</div><h3>${escapeHtml(view)}</h3><p>View not implemented</p></div>`;
  }
}

function setRightPanel(html) {
  document.getElementById('right-content').innerHTML = html;
  S.rightPanelContent = true;
}

function clearRightPanel() {
  document.getElementById('right-content').innerHTML = '';
  S.rightPanelContent = false;
}

function formatTime(ts) {
  if (!ts) return '';
  const d = typeof ts === 'number' ? new Date(ts) : new Date(ts);
  return d.toLocaleTimeString();
}

function formatDate(ts) {
  if (!ts) return '';
  const d = typeof ts === 'number' ? new Date(ts) : new Date(ts);
  return d.toLocaleDateString() + ' ' + d.toLocaleTimeString();
}

function durationStr(seconds) {
  if (!seconds) return '0s';
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = Math.floor(seconds % 60);
  const parts = [];
  if (h) parts.push(`${h}h`);
  if (m) parts.push(`${m}m`);
  if (s) parts.push(`${s}s`);
  return parts.join(' ') || '0s';
}

function capitalize(str) {
  return str.charAt(0).toUpperCase() + str.slice(1);
}

function renderEmailView(area) {
  area.innerHTML = `
    <div class="health-grid">
      <div class="health-card good">
        <div class="label">SMTP Status</div>
        <div class="value">Ready</div>
      </div>
      <div class="health-card good">
        <div class="label">Mailchain</div>
        <div class="value">Configured</div>
      </div>
      <div class="health-card good">
        <div class="label">Registered Addresses</div>
        <div class="value" id="email-count">0</div>
      </div>
    </div>
    <div class="quick-actions">
      <button class="btn btn-primary" onclick="showSendEmail()">&#9993; Send Email</button>
      <button class="btn btn-secondary" onclick="showEmailInbox()">&#9776; Inbox</button>
      <button class="btn btn-secondary" onclick="showRegisterEmail()">+ Register Address</button>
    </div>
    <div id="email-list"></div>`;
  loadEmailAddresses();
}

async function loadEmailAddresses() {
  const res = await api('/api/email/addresses');
  if (!res) return;
  const list = document.getElementById('email-list');
  document.getElementById('email-count').textContent = res.addresses?.length || 0;
  if (!res.addresses?.length) {
    list.innerHTML = `<div class="empty-state"><div class="icon">&#9993;</div><h3>No Email Addresses</h3><p>Register an email address for your agents to start sending messages.</p></div>`;
    return;
  }
  list.innerHTML = `<table><tr><th>Agent</th><th>Address</th><th>SMTP</th><th>Mailchain</th><th>Actions</th></tr>
    ${res.addresses.map(a => `<tr>
      <td>${escapeHtml(a.agent_name)}</td>
      <td>${escapeHtml(a.email_address)}</td>
      <td>${a.smtp_enabled ? '&#10003;' : '&#10007;'}</td>
      <td>${a.mailchain_enabled ? '&#10003;' : '&#10007;'}</td>
      <td><button class="btn btn-secondary" style="padding:2px 8px;font-size:11px;" onclick="removeEmail('${escapeHtml(a.agent_name)}')">Remove</button></td>
    </tr>`).join('')}</table>`;
}

function showSendEmail() {
  const content = document.getElementById('right-content');
  content.innerHTML = `
    <h3 style="margin-bottom:12px;">Send Email</h3>
    <div style="display:flex;flex-direction:column;gap:10px;">
      <div><label>To</label><input type="text" id="email-to" placeholder="recipient@example.com"></div>
      <div><label>Subject</label><input type="text" id="email-subject" placeholder="Subject"></div>
      <div><label>Body</label><textarea id="email-body" rows="4" style="resize:vertical;" placeholder="Message body"></textarea></div>
      <div><label>Mode</label><select id="email-mode"><option value="smtp">SMTP</option><option value="mailchain">Mailchain</option></select></div>
      <button class="btn btn-primary" onclick="sendEmail()">Send</button>
    </div>`;
  S.rightPanelContent = true;
}

async function sendEmail() {
  const to = document.getElementById('email-to').value;
  const subject = document.getElementById('email-subject').value;
  const body = document.getElementById('email-body').value;
  const mode = document.getElementById('email-mode').value;
  if (!to || !subject || !body) { showToast('All fields required', 2000, 'error'); return; }
  const res = await api('/api/email/send', {
    method: 'POST',
    body: { to, subject, body, mode },
  });
  if (res) showToast(`Email ${res.status}: ${res.id}`, 3000, res.status === 'sent' ? 'success' : 'error');
}

function showRegisterEmail() {
  const content = document.getElementById('right-content');
  content.innerHTML = `
    <h3 style="margin-bottom:12px;">Register Email Address</h3>
    <div style="display:flex;flex-direction:column;gap:10px;">
      <div><label>Agent Name</label><input type="text" id="reg-agent" placeholder="agnetic-core"></div>
      <div><label>Email Address</label><input type="text" id="reg-address" placeholder="agent@agnetic.local"></div>
      <div><label><input type="checkbox" id="reg-smtp" checked> SMTP Enabled</label></div>
      <button class="btn btn-primary" onclick="registerEmail()">Register</button>
    </div>`;
  S.rightPanelContent = true;
}

async function registerEmail() {
  const agent = document.getElementById('reg-agent').value;
  const address = document.getElementById('reg-address').value;
  const smtp = document.getElementById('reg-smtp').checked;
  if (!agent || !address) { showToast('Agent name and address required', 2000, 'error'); return; }
  const res = await api('/api/email/register', {
    method: 'POST',
    body: { agent, address, smtp_enabled: smtp },
  });
  if (res) { showToast(`Registered ${address} for ${agent}`, 3000, 'success'); loadEmailAddresses(); }
}

async function removeEmail(agent) {
  const res = await api('/api/email/remove', {
    method: 'POST',
    body: { agent },
  });
  if (res && res.removed) { showToast(`Removed ${agent}`, 2000, 'success'); loadEmailAddresses(); }
}
