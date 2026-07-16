/* Shield — multi-node telemetry + agent deployment */

async function renderShieldView(area) {
  showLoading(area);
  const [stats, installerInfo] = await Promise.all([
    api('/api/shield/stats'),
    api('/api/agent/installer-info'),
  ]);

  if (!stats || stats.status === 'no_data') {
    area.innerHTML = `
      <div class="view-header">
        <h2><span>Shield</span> · Fleet Security Telemetry</h2>
      </div>
      <div class="empty-state glass" style="margin-bottom:16px">
        <div class="icon" style="font-size:32px">⛨</div>
        <h3>No endpoints yet</h3>
        <p>Deploy StarAgent on remote machines to collect telemetry.</p>
      </div>
      ${renderDownloadSection(installerInfo)}
    `;
    return;
  }

  const agg = stats.aggregate || {};
  const nodes = stats.nodes || [];
  const online = nodes.filter(n => n.tables && n.tables.status);

  area.innerHTML = `
    <div class="view-header">
      <h2><span>Shield</span> · Fleet Security Telemetry</h2>
      <span class="muted mono" style="font-size:11px">${escapeHtml(stats.timestamp || '')}</span>
    </div>

    <div class="health-grid">
      <div class="health-card glass ${agg.cpu_avg > 80 ? 'bad' : agg.cpu_avg > 60 ? 'warn' : 'good'}">
        <div class="label">Avg CPU</div>
        <div class="value">${agg.cpu_avg || 0}%</div>
      </div>
      <div class="health-card glass ${agg.memory_percent_avg > 80 ? 'bad' : agg.memory_percent_avg > 60 ? 'warn' : 'good'}">
        <div class="label">Avg Memory</div>
        <div class="value">${agg.memory_percent_avg || 0}%</div>
      </div>
      <div class="health-card glass ${agg.disk_percent_avg > 80 ? 'bad' : agg.disk_percent_avg > 60 ? 'warn' : 'good'}">
        <div class="label">Avg Disk</div>
        <div class="value">${agg.disk_percent_avg || 0}%</div>
      </div>
      <div class="health-card glass ${online.length > 0 ? 'good' : 'warn'}">
        <div class="label">Nodes Online</div>
        <div class="value">${online.length}/${stats.total_nodes}</div>
      </div>
      <div class="health-card glass good">
        <div class="label">Peak CPU</div>
        <div class="value">${agg.cpu_max || 0}%</div>
      </div>
    </div>

    <div class="section glass">
      <div class="panel-title">Endpoints (${nodes.length})</div>
      <div style="padding:0 14px 14px">
        ${nodes.length ? `<table>
          <tr><th>Hostname</th><th>CPU</th><th>Memory</th><th>Disk</th><th>Network</th><th>Last Seen</th></tr>
          ${nodes.map(n => {
            const s = (n.tables && n.tables.status) || {};
            const cpu = typeof s.cpu === 'number' ? s.cpu.toFixed(1) + '%' : '—';
            const mem = typeof s.memory_used === 'number' && typeof s.memory_total === 'number'
              ? ((s.memory_used / s.memory_total) * 100).toFixed(1) + '%' : '—';
            const disk = typeof s.disk_used === 'number' && typeof s.disk_total === 'number'
              ? ((s.disk_used / s.disk_total) * 100).toFixed(1) + '%' : '—';
            const net = s.rx_bytes != null || s.tx_bytes != null
              ? '↓' + formatBytes(s.rx_bytes || 0) + ' ↑' + formatBytes(s.tx_bytes || 0) : '—';
            return `<tr>
              <td><strong>${escapeHtml(n.hostname)}</strong></td>
              <td><span class="badge ${parseFloat(cpu) > 80 ? 'danger' : parseFloat(cpu) > 60 ? 'warn' : ''}">${cpu}</span></td>
              <td>${mem}</td>
              <td>${disk}</td>
              <td class="mono" style="font-size:10px">${net}</td>
              <td class="muted mono" style="font-size:10px">${escapeHtml(n.last_seen || '')}</td>
            </tr>`;
          }).join('')}
        </table>` : '<div class="empty-state" style="padding:16px"><p>No endpoints reporting</p></div>'}
      </div>
    </div>

    <div class="section glass">
      <div class="panel-title">Per-Node Detail</div>
      <div style="padding:0 14px 14px">
        ${online.length ? online.map(n => {
          const s = n.tables.status || {};
          const cpu = typeof s.cpu === 'number' ? s.cpu.toFixed(1) : '—';
          const memUsed = formatBytes(s.memory_used || 0);
          const memTotal = formatBytes(s.memory_total || 0);
          const diskUsed = formatBytes(s.disk_used || 0);
          const diskTotal = formatBytes(s.disk_total || 0);
          return `<div class="plant-card glass" style="margin-bottom:8px">
            <div class="plant-name">${escapeHtml(n.hostname)}</div>
            <div class="plant-meta">
              <span>CPU: ${cpu}%</span>
              <span>RAM: ${memUsed} / ${memTotal}</span>
              <span>Disk: ${diskUsed} / ${diskTotal}</span>
              <span>RX: ${formatBytes(s.rx_bytes || 0)}</span>
              <span>TX: ${formatBytes(s.tx_bytes || 0)}</span>
            </div>
            <div class="plant-meta" style="margin-top:4px">
              <span class="badge">${Object.keys(n.tables).length} table(s)</span>
              <span class="muted mono" style="font-size:10px">${escapeHtml(n.last_seen || '')}</span>
            </div>
          </div>`;
        }).join('') : '<div class="empty-state" style="padding:16px"><p>No nodes reporting</p></div>'}
      </div>
    </div>

    ${renderDownloadSection(installerInfo)}
  `;
}

function renderDownloadSection(info) {
  if (!info) return '';
  const plat = info.platforms || {};
  const natsUrl = info.nats_url || 'nats://hub:4222';
  const token = info.token || '';

  const platKeys = Object.keys(plat);
  const platHtml = platKeys.map(key => {
    const p = plat[key];
    const icon = key === 'windows' ? '⊞' : key === 'darwin' ? '⌘' : '⌨';
    return `<div class="plant-card glass" style="cursor:pointer;text-align:center" onclick="downloadAgent('${escapeHtml(key)}')">
      <div style="font-size:28px;margin-bottom:6px">${icon}</div>
      <div class="plant-name">${escapeHtml(p.name)}</div>
      <div class="plant-meta" style="justify-content:center">
        <span class="badge">Download</span>
      </div>
    </div>`;
  }).join('') || '<div class="muted" style="padding:8px">No platforms available</div>';

  return `
    <div class="section glass" style="margin-top:16px">
      <div class="panel-title">Deploy Agent</div>
      <div style="padding:0 14px 14px">
        <div style="display:flex;gap:10px;align-items:center;margin-bottom:12px;flex-wrap:wrap">
          <div style="flex:1;min-width:200px">
            <div style="font-size:11px;color:var(--color-text-muted);margin-bottom:4px">NATS Hub URL</div>
            <div style="display:flex;gap:4px;align-items:center">
              <code class="mono" style="font-size:13px;background:var(--color-glass);padding:6px 10px;border-radius:6px;border:1px solid var(--color-glass-edge);flex:1">${escapeHtml(natsUrl)}</code>
              <span style="cursor:pointer;font-size:16px;padding:4px;border-radius:4px;opacity:0.7" title="Copy NATS URL" data-value="${escapeHtml(natsUrl)}" onclick="copyValue(this.dataset.value)" onmouseover="this.style.opacity='1'" onmouseout="this.style.opacity='0.7'">📋</span>
            </div>
          </div>
          <div style="flex:1;min-width:200px">
            <div style="font-size:11px;color:var(--color-text-muted);margin-bottom:4px">Agent Token</div>
            <div style="display:flex;gap:4px;align-items:center">
              <code class="mono" style="font-size:13px;background:var(--color-glass);padding:6px 10px;border-radius:6px;border:1px solid var(--color-glass-edge);flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${escapeHtml(token)}">${escapeHtml(token.substring(0, 24))}…</code>
              <span style="cursor:pointer;font-size:16px;padding:4px;border-radius:4px;opacity:0.7" title="Copy token" data-value="${escapeHtml(token)}" onclick="copyValue(this.dataset.value)" onmouseover="this.style.opacity='1'" onmouseout="this.style.opacity='0.7'">📋</span>
            </div>
          </div>
          <button class="btn btn-secondary" style="padding:6px 14px;font-size:12px" onclick="regenerateToken()">Regenerate Token</button>
        </div>

        <div style="font-size:11px;color:var(--color-text-muted);margin-bottom:8px">Download installer for your platform:</div>
        <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:10px">
          ${platHtml}
        </div>

        <div style="margin-top:12px;padding-top:10px;border-top:1px solid var(--color-glass-edge)">
          <div style="font-size:11px;color:var(--color-text-muted);margin-bottom:6px">Or install with a single command:</div>
          <div style="display:flex;gap:6px;flex-wrap:wrap">
            <code id="install-cmd" class="mono" style="flex:1;font-size:12px;background:var(--color-glass);padding:8px 12px;border-radius:6px;border:1px solid var(--color-glass-edge);word-break:break-all">curl -fsSL https://github.com/andromi-hash/starship-os/raw/master/scripts/install-agent-linux.sh | sudo bash -s -- --nats-url ${escapeHtml(natsUrl)} --nats-token ${escapeHtml(token)} --download-url ${escapeHtml(window.location.origin)}/api/agent/download</code>
            <button class="btn btn-secondary" style="padding:6px 14px;font-size:12px;flex-shrink:0" onclick="copyInstallCmd()">Copy</button>
          </div>
        </div>
      </div>
    </div>
  `;
}

async function downloadAgent(platform) {
  const info = await api('/api/agent/installer-info');
  if (!info) { showToast('Failed to get installer info', 3000, 'error'); return; }
  const url = `/api/agent/download/${encodeURIComponent(platform)}?token=${encodeURIComponent(info.token)}`;
  window.location.href = url;
}

async function regenerateToken() {
  if (!confirm('Regenerate the shared agent token? Existing agents using the old token will lose connection until updated with the new one.')) return;
  const res = await api('/api/agent/regenerate-token', { method: 'POST' });
  if (res && res.status === 'ok') {
    showToast('New token generated. Update your agents with the new token.', 4000, 'info');
    const area = document.getElementById('shield-content');
    if (area && S.currentView === 'shield') renderShieldView(area);
  } else {
    showToast('Failed to regenerate token', 3000, 'error');
  }
}

function copyValue(text) {
  navigator.clipboard.writeText(text).then(() => {
    showToast('Copied to clipboard', 2000, 'success');
  }).catch(() => {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    document.execCommand('copy');
    document.body.removeChild(ta);
    showToast('Copied to clipboard', 2000, 'success');
  });
}

function copyInstallCmd() {
  const el = document.getElementById('install-cmd');
  if (!el) return;
  copyValue(el.textContent);
}
