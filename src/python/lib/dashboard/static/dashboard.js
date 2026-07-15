/* ── Dashboard View — Airia-style health cards ─────────────────────────── */

async function renderDashboard(area) {
  showLoading(area);
  const [health, healerRes] = await Promise.all([
    api('/api/health'),
    api('/api/healer'),
  ]);
  if (!health) { showError(area, 'Failed to load system status'); return; }

  const sys = health.system || {};
  const agents = health.agents || {};
  const incidents = health.incidents || {};
  const healer = healerRes?.summary || {};

  const cpuCard = sys.cpu?.pct < 70 ? 'good' : sys.cpu?.pct < 90 ? 'warn' : 'critical';
  const memCard = sys.memory?.pct < 70 ? 'good' : sys.memory?.pct < 85 ? 'warn' : 'critical';
  const diskCard = sys.disk?.pct < 70 ? 'good' : sys.disk?.pct < 85 ? 'warn' : 'critical';
  const agentCard = agents.offline > 0 ? 'warn' : 'good';

  area.innerHTML = `
    <div class="health-grid">
      <div class="health-card ${cpuCard}">
        <div class="label">CPU</div>
        <div class="value">${Math.round(sys.cpu?.pct || 0)}%</div>
      </div>
      <div class="health-card ${memCard}">
        <div class="label">Memory</div>
        <div class="value">${Math.round(sys.memory?.pct || 0)}%</div>
      </div>
      <div class="health-card ${diskCard}">
        <div class="label">Disk</div>
        <div class="value">${Math.round(sys.disk?.pct || 0)}%</div>
      </div>
      <div class="health-card ${agentCard}">
        <div class="label">Agents</div>
        <div class="value">${agents.online || 0}</div>
      </div>
      <div class="health-card ${incidents.critical ? 'critical' : 'good'}">
        <div class="label">Incidents</div>
        <div class="value">${incidents.open || 0}</div>
      </div>
      <div class="health-card good">
        <div class="label">Recoveries</div>
        <div class="value">${healer.recoveries_performed || 0}</div>
      </div>
    </div>

    <div class="quick-actions">
      <button class="btn btn-primary" onclick="renderView('chat')">&#9993; Chat with Agents</button>
      <button class="btn btn-secondary" onclick="renderView('agents')">&#9679; Agent Status</button>
      <button class="btn btn-secondary" onclick="renderView('incidents')">&#9888; Incidents</button>
      <button class="btn btn-secondary" onclick="renderView('orgchart')">&#9632; Org Chart</button>
      <button class="btn btn-secondary" onclick="renderView('goals')">&#9733; Goals</button>
      <button class="btn btn-secondary" onclick="renderView('shield')">&#9733; Scan Secrets</button>
    </div>

    <div class="card">
      <h3>&#9881; System Overview</h3>
      <p>Starship OS v${escapeHtml(health.version)} &middot; ${durationStr(health.uptime_seconds)} uptime &middot; ${agents.total || 0} agents (${agents.online || 0} online, ${agents.busy || 0} busy, ${agents.offline || 0} offline)</p>
    </div>

    <div class="card">
      <h3>&#9888; Incidents by Severity</h3>
      <p>
        ${Object.entries(incidents.by_severity || {}).map(([s, c]) =>
          `<span class="severity-${s}">${c} ${s}</span>`
        ).join(' &middot; ') || 'No incidents'}
      </p>
    </div>

    <div class="card">
      <h3>&#9772; Active Goals</h3>
      <div id="dash-goals-feed">Loading...</div>
    </div>
  `;

  // Load goals for dashboard
  const goalsRes = await api('/api/orgchart');
  if (goalsRes?.goals) {
    const feed = document.getElementById('dash-goals-feed');
    feed.innerHTML = goalsRes.goals.map(g => `
      <div style="display:flex;align-items:center;gap:12px;margin:4px 0;padding:6px 0;border-bottom:1px solid rgba(255,255,255,0.04);">
        <span style="width:100px;font-size:12px;color:rgba(255,255,255,0.5)">${escapeHtml(g.title)}</span>
        <div style="flex:1;height:6px;background:rgba(255,255,255,0.06);border-radius:3px;overflow:hidden;">
          <div style="width:${g.progress}%;height:100%;background:linear-gradient(90deg,#8b7bd6,#6c5bbf);border-radius:3px;transition:width 0.5s;"></div>
        </div>
        <span style="width:40px;text-align:right;font-size:12px;font-weight:600;color:#c4b5f5">${g.progress}%</span>
        <span style="font-size:11px;color:${g.status === 'active' ? '#00e676' : 'rgba(255,255,255,0.3)'}">${g.status}</span>
      </div>
    `).join('');
  }
}
