/* Crew Manifest — live agents from YAML + process status */

async function renderAgentView(area) {
  showLoading(area);
  const res = await api('/api/agents');
  const list = (res && (res.agents || [])) || [];
  S.agents = Array.isArray(list) ? list : Object.values(list);
  if (res && res.agents_map) S.agentsMap = res.agents_map;
  else {
    S.agentsMap = {};
    S.agents.forEach(a => { S.agentsMap[a.name] = a; });
  }
  renderSidebar();

  if (!S.agents.length) {
    showNoData(area, 'No crew configured', 'No agent YAML configs found under agents/.');
    return;
  }

  area.innerHTML = `
    <div class="view-header">
      <h2><span>Crew Manifest</span></h2>
      <span class="muted" style="font-size:12px">${S.agents.filter(a => a.running || a.status==='online').length} online · ${S.agents.length} total</span>
    </div>
    <div class="crew-grid">
      ${S.agents.map(a => {
        const on = a.running || a.status === 'online';
        const skills = (a.skills || []).slice(0, 4);
        return `<div class="crew-card glass" onclick="selectCrew('${escapeHtml(a.name)}')">
          <div class="row">
            <span class="status-dot ${on ? 'green' : 'red'}"></span>
            <span class="name">${escapeHtml(a.name)}</span>
            <span class="badge ${on ? 'ok' : ''}" style="margin-left:auto">${on ? 'online' : 'offline'}</span>
          </div>
          <div class="meta">${escapeHtml(a.model || 'unknown model')}</div>
          <div class="meta" style="margin-top:4px">${escapeHtml(a.role || a.description || '')}</div>
          <div class="skills">${skills.map(s => `<span class="chip">${escapeHtml(s)}</span>`).join('')}</div>
        </div>`;
      }).join('')}
    </div>
  `;
}

function selectCrew(name) {
  const a = (S.agentsMap && S.agentsMap[name]) || (S.agents || []).find(x => x.name === name);
  if (!a) {
    setRightPanel(`<div class="empty-state"><p>No data for ${escapeHtml(name)}</p></div>`);
    return;
  }
  const on = a.running || a.status === 'online';
  const caps = a.capabilities || [];
  const skills = a.skills || [];
  setRightPanel(`
    <div style="margin-bottom:12px">
      <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px">
        <span class="status-dot ${on ? 'green' : 'red'}"></span>
        <strong style="font-size:16px">${escapeHtml(a.name)}</strong>
      </div>
      <div class="muted mono" style="font-size:11px;margin-bottom:8px">${escapeHtml(a.model || '')}</div>
      <div style="font-size:12px;color:var(--muted)">${escapeHtml(a.role || a.description || '')}</div>
    </div>
    <div class="panel-title" style="padding-left:0">Capabilities</div>
    <div style="margin-bottom:12px">
      ${caps.length ? caps.map(c => `<span class="chip" style="margin:2px">${escapeHtml(c)}</span>`).join('') : '<span class="muted">None listed</span>'}
    </div>
    <div class="panel-title" style="padding-left:0">Skills</div>
    <div style="margin-bottom:12px">
      ${skills.length ? skills.map(c => `<span class="chip" style="margin:2px">${escapeHtml(c)}</span>`).join('') : '<span class="muted">None listed</span>'}
    </div>
    <div style="display:flex;flex-direction:column;gap:8px;margin-top:16px">
      <button class="btn btn-primary" onclick="S.chatAgent='${escapeHtml(a.name)}'; renderView('chat')">Check In →</button>
      <button class="btn btn-secondary" onclick="quickPing('${escapeHtml(a.name)}')">Ping via NATS</button>
    </div>
  `);
}

async function quickPing(name) {
  showToast(`Pinging ${name}…`, 2000);
  const res = await api('/api/send', { method: 'POST', body: { agent: name, command: 'ping', args: {} } });
  if (!res) { showToast('Ping failed', 3000, 'error'); return; }
  if (res.error) showToast(res.error, 4000, 'error');
  else showToast(`${name}: ${res.response || res.status || 'ok'}`, 4000, 'success');
  setRightPanel((document.getElementById('right-content').innerHTML || '') +
    `<pre class="mono" style="margin-top:12px;font-size:11px;white-space:pre-wrap;color:var(--muted)">${escapeHtml(JSON.stringify(res, null, 2))}</pre>`);
}
