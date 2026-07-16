/* Overview — live telemetry, GPU, models, crew summary */

async function renderDashboard(area) {
  showLoading(area);
  const data = await api('/api/dashboard');
  S.dashboard = data;
  if (!data) {
    showError(area, 'Dashboard API unavailable');
    return;
  }

  const t = data.telemetry || {};
  const cpu = Number(t.cpu_percent || 0).toFixed(0);
  const mem = Number(t.memory_percent || 0).toFixed(1);
  const disk = Number(t.disk_percent || 0).toFixed(1);
  const agents = data.agents || {};
  const online = Object.values(agents).filter(a => a.running || a.status === 'online').length;
  const total = Object.keys(agents).length;
  const gpu = data.gpu || {};
  const models = (data.ollama && data.ollama.models) || [];
  const nats = data.nats || {};

  const ring = (id, pct, color) => {
    const p = Math.max(0, Math.min(100, Number(pct) || 0));
    return `<div class="gauge">
      <div class="gauge-ring">
        <svg viewBox="0 0 36 36">
          <circle class="gauge-bg" cx="18" cy="18" r="15.9"/>
          <circle class="gauge-fg" cx="18" cy="18" r="15.9" stroke="${color}"
            stroke-dasharray="${p} ${100 - p}" style="color:${color}"/>
        </svg>
        <div class="gauge-val">${p}%</div>
      </div>
      <div class="gauge-label">${id}</div>
    </div>`;
  };

  area.innerHTML = `
    <div class="view-header">
      <h2><span>Overview</span> · Command Picture</h2>
      <span class="muted mono" style="font-size:11px">${escapeHtml(data.timestamp || '')}</span>
    </div>

    <div class="health-grid">
      <div class="health-card glass ${pctClass(cpu)}">
        <div class="label">CPU</div>
        <div class="value">${cpu}%</div>
      </div>
      <div class="health-card glass ${pctClass(mem)}">
        <div class="label">Memory</div>
        <div class="value">${mem}%</div>
      </div>
      <div class="health-card glass ${pctClass(disk)}">
        <div class="label">Disk</div>
        <div class="value">${disk}%</div>
      </div>
      <div class="health-card glass ${online ? 'good' : 'bad'}">
        <div class="label">Crew Online</div>
        <div class="value">${online}/${total}</div>
      </div>
      <div class="health-card glass ${nats.connected ? 'good' : 'warn'}">
        <div class="label">NATS</div>
        <div class="value" style="font-size:16px;padding-top:8px">${nats.connected ? 'LINK' : 'DOWN'}</div>
      </div>
      <div class="health-card glass good">
        <div class="label">Models</div>
        <div class="value">${models.length}</div>
      </div>
    </div>

    <div class="section glass">
      <div class="panel-title">Telemetry Rings</div>
      <div class="gauges">
        ${ring('CPU', cpu, 'var(--cyan)')}
        ${ring('RAM', mem, 'var(--green)')}
        ${ring('Disk', disk, 'var(--orange)')}
      </div>
    </div>

    <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px">
      <div class="section glass">
        <div class="panel-title">GPU</div>
        <div style="padding:0 14px 14px;font-size:13px">
          ${gpu.vendor && gpu.vendor !== 'none' ? `
            <div><strong>${escapeHtml(gpu.name || gpu.vendor)}</strong></div>
            <div class="muted mono" style="margin-top:6px;font-size:11px">
              ${escapeHtml(gpu.vendor || '')}
              ${gpu.vram_mb ? ` · ${gpu.vram_mb} MB VRAM` : ''}
              ${gpu.driver ? ` · driver ${escapeHtml(gpu.driver)}` : ''}
              ${gpu.cuda ? ` · CUDA ${escapeHtml(String(gpu.cuda))}` : ''}
            </div>
          ` : `<div class="empty-state" style="padding:16px"><p>No GPU data</p></div>`}
        </div>
      </div>
      <div class="section glass">
        <div class="panel-title">Ollama Models</div>
        <div style="padding:0 14px 14px;max-height:160px;overflow:auto">
          ${models.length ? models.map(m => `
            <div style="display:flex;justify-content:space-between;padding:4px 0;border-bottom:1px solid rgba(255,255,255,0.04);font-size:12px">
              <span class="mono">${escapeHtml(m.name)}</span>
              <span class="muted">${m.size ? formatBytes(m.size) : ''}</span>
            </div>`).join('') : `<div class="empty-state" style="padding:16px"><p>No models loaded</p></div>`}
        </div>
      </div>
    </div>

    <div class="section glass">
      <div class="panel-title">Crew Snapshot</div>
      <div style="padding:0 14px 14px">
        ${total ? Object.values(agents).map(a => `
          <div style="display:flex;align-items:center;gap:8px;padding:6px 0;border-bottom:1px solid rgba(255,255,255,0.04);font-size:12px;cursor:pointer"
               onclick="renderView('agents'); setTimeout(()=>selectCrew('${escapeHtml(a.name)}'),50)">
            <span class="status-dot ${a.running || a.status==='online' ? 'green' : 'red'}"></span>
            <strong style="min-width:100px">${escapeHtml(a.name)}</strong>
            <span class="muted mono">${escapeHtml(a.model || '')}</span>
            <span class="muted" style="margin-left:auto">${a.running || a.status==='online' ? 'RUNNING' : 'IDLE'}</span>
          </div>`).join('') : showNoDataHTML('No crew configured')}
      </div>
    </div>
  `;
}

function showNoDataHTML(msg) {
  return `<div class="empty-state" style="padding:16px"><p>${escapeHtml(msg)}</p></div>`;
}
