/* Fleet Map — plants, nodes, exercise control */

async function renderFleetView(area) {
  showLoading(area);
  const data = await api('/api/fleet');
  S.fleet = data;
  if (!data || data.status === 'no_data') {
    showNoData(area, 'No fleet data', 'fleet.yaml / fleet-state.json not available.');
    return;
  }

  const plants = data.plants || [];
  const exercise = data.exercise || {};
  const nodes = data.nodes || [];

  area.innerHTML = `
    <div class="view-header">
      <h2><span>Fleet Map</span> · ${escapeHtml(data.fleet || 'starship-fleet')}</h2>
      <span class="muted mono" style="font-size:11px">${escapeHtml(data.updated || data.timestamp || '')}</span>
    </div>

    ${exercise.active ? `<div class="exercise-banner glass">EXERCISE ACTIVE${exercise.plant ? ' · ' + escapeHtml(exercise.plant) : ''}${exercise.started ? ' · since ' + escapeHtml(exercise.started) : ''}</div>` : ''}

    <div class="fleet-controls">
      <button class="btn btn-primary" onclick="fleetExercise('start')">Exercise Start</button>
      <button class="btn btn-secondary" onclick="fleetExercise('stop')">Exercise Stop</button>
      <button class="btn btn-secondary" onclick="fleetRegister()">Register Node</button>
      <button class="btn btn-secondary" onclick="renderFleetView(document.getElementById('fleet-content'))">Refresh</button>
    </div>

    <div class="plant-grid">
      ${plants.length ? plants.map(p => `
        <div class="plant-card glass ${p.isolation ? 'isolated' : ''}" onclick="showPlantDetail('${escapeHtml(p.id)}')">
          <div class="plant-name">${escapeHtml(p.name || p.id)}</div>
          <div class="plant-meta">
            <span class="badge">${escapeHtml(p.profile || '—')}</span>
            <span>${escapeHtml(p.region || '—')}</span>
            <span>${p.node_count || 0} node(s)</span>
            ${p.isolation ? '<span class="badge danger">isolated</span>' : ''}
          </div>
          <div class="node-list">
            ${(p.nodes || []).slice(0, 5).map(n => `
              <div class="node-row">
                <strong>${escapeHtml(n.hostname || n.node_id || '?')}</strong>
                <span>${escapeHtml(n.status || '—')} · ${(n.roles || []).join(', ')}</span>
              </div>`).join('') || '<div class="muted" style="padding-top:8px">No nodes registered</div>'}
          </div>
        </div>`).join('') : '<div class="empty-state glass" style="grid-column:1/-1"><h3>No plants</h3><p>Define plants in /etc/starship/fleet.yaml</p></div>'}
    </div>

    <div class="section glass" style="margin-top:16px">
      <div class="panel-title">All Nodes (${nodes.length})</div>
      <div style="padding:0 14px 14px">
        ${nodes.length ? `<table>
          <tr><th>Node</th><th>Plant</th><th>Roles</th><th>Status</th><th>Last Seen</th></tr>
          ${nodes.map(n => `<tr>
            <td><strong>${escapeHtml(n.hostname || n.node_id)}</strong></td>
            <td>${escapeHtml(n.plant || '—')}</td>
            <td class="mono" style="font-size:11px">${escapeHtml((n.roles || []).join(', '))}</td>
            <td><span class="badge ${n.status === 'online' ? 'ok' : ''}">${escapeHtml(n.status || '—')}</span></td>
            <td class="muted mono" style="font-size:10px">${escapeHtml(n.last_seen || '—')}</td>
          </tr>`).join('')}
        </table>` : '<div class="empty-state" style="padding:16px"><p>No active nodes</p></div>'}
      </div>
    </div>
  `;
}

function showPlantDetail(id) {
  const p = (S.fleet && S.fleet.plants || []).find(x => x.id === id);
  if (!p) return;
  setRightPanel(`
    <div style="margin-bottom:10px">
      <strong style="font-size:16px">${escapeHtml(p.name || p.id)}</strong>
      ${p.isolation ? ' <span class="badge danger">isolated</span>' : ''}
    </div>
    <div class="muted" style="font-size:12px;margin-bottom:10px">
      Profile: ${escapeHtml(p.profile || '—')}<br>
      Region: ${escapeHtml(p.region || '—')}<br>
      Roles: ${escapeHtml((p.roles_allowed || []).join(', ') || '—')}
    </div>
    <div class="panel-title" style="padding-left:0">Nodes</div>
    ${(p.nodes || []).map(n => `
      <div style="padding:6px 0;border-bottom:1px solid rgba(255,255,255,0.05);font-size:12px">
        <strong>${escapeHtml(n.hostname || n.node_id)}</strong>
        <div class="muted mono" style="font-size:10px">${escapeHtml((n.roles || []).join(', '))}</div>
      </div>`).join('') || '<p class="muted">No nodes</p>'}
  `);
}

async function fleetExercise(action) {
  const res = await api('/api/fleet/exercise', { method: 'POST', body: { action } });
  if (!res || res.error) {
    showToast(res?.error || 'Exercise command failed', 3000, 'error');
    return;
  }
  showToast(`Exercise ${action}`, 2500, 'success');
  const area = document.getElementById('fleet-content');
  if (area && S.currentView === 'fleet') renderFleetView(area);
}

async function fleetRegister() {
  showToast('Registering node…', 2000);
  const res = await api('/api/fleet/register', { method: 'POST', body: {} });
  if (!res) { showToast('Register failed', 3000, 'error'); return; }
  if (res.ok) showToast('Node registered', 3000, 'success');
  else showToast(res.error || res.stderr || 'Register failed', 4000, 'error');
  const area = document.getElementById('fleet-content');
  if (area && S.currentView === 'fleet') renderFleetView(area);
}
