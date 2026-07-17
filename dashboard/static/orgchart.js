/* Org Chart — agent hierarchy */

async function renderOrgChartView(area) {
  showLoading(area);
  const res = await api('/api/orgchart');
  if (!res || !res.hierarchy) {
    showNoData(area, 'Org Chart', 'No organization data available.');
    return;
  }

  const hier = res.hierarchy;
  const subs = res.sub_agents || {};

  // Build hierarchy lines recursively
  function renderNode(id, node, depth) {
    if (!node) return '';
    const statusDot = node.online
      ? '<span class="status-dot green" style="display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--color-success);box-shadow:0 0 6px rgba(0,204,136,0.4);margin-right:6px"></span>'
      : '<span class="status-dot red" style="display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--color-alert);box-shadow:0 0 6px rgba(255,51,85,0.4);margin-right:6px"></span>';
    const connectors = node.connectors && node.connectors.length
      ? `<div style="margin-top:4px;display:flex;gap:4px;flex-wrap:wrap">${node.connectors.map(c => `<span class="badge" style="font-size:9px">${escapeHtml(c)}</span>`).join('')}</div>`
      : '';
    return `
      <div style="margin-left:${depth * 24}px;margin-bottom:${depth === 0 ? '16px' : '8px'}">
        <div class="plant-card glass" style="${depth === 0 ? 'border-color:var(--color-primary);border-width:2px' : ''}">
          <div style="display:flex;align-items:center;gap:8px">
            ${statusDot}
            <div style="flex:1">
              <div class="plant-name">${escapeHtml(node.name || id)}</div>
              <div style="font-size:11px;color:var(--color-text-dim)">${escapeHtml(node.role || '')}</div>
            </div>
            <span class="badge" style="font-size:10px">${escapeHtml(node.model || '')}</span>
          </div>
          <div style="margin-top:6px;font-size:12px;color:var(--color-text-muted);line-height:1.4">${escapeHtml(node.description || '')}</div>
          ${connectors}
        </div>
        ${(node.children || []).map(childId => {
          const child = hier[childId] || subs[childId];
          if (!child) return '';
          return renderNode(childId, child, depth + 1)
            + (hier[childId] ? '' : '');
        }).join('')}
      </div>
    `;
  }

  const root = hier.romi;
  area.innerHTML = `
    <div class="view-header">
      <h2><span>Org Chart</span> · Agent Hierarchy</h2>
      <span class="muted">${escapeHtml(res.timestamp || '')}</span>
    </div>
    <div class="section glass">
      <div class="panel-title">Command Structure</div>
      <div style="padding:0 14px 14px">
        <div style="position:relative">
          ${renderNode('romi', root, 0)}
        </div>
      </div>
    </div>
    <div class="section glass" style="margin-top:12px">
      <div class="panel-title">Sub-Agents (under Proxy)</div>
      <div style="padding:0 14px 14px">
        <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:8px">
          ${Object.entries(subs).map(([id, info]) => `
            <div class="plant-card glass" style="padding:10px">
              <div style="display:flex;align-items:center;gap:6px;margin-bottom:4px">
                <span style="display:inline-block;width:6px;height:6px;border-radius:50%;background:${info.online ? 'var(--color-success)' : 'var(--color-alert)'}"></span>
                <strong style="font-size:12px">${escapeHtml(id)}</strong>
              </div>
              <div style="font-size:10px;color:var(--color-text-dim)">${escapeHtml(info.role || '')}</div>
              <div style="font-size:10px;color:var(--color-text-muted);margin-top:2px">${escapeHtml(info.description || '')}</div>
            </div>
          `).join('')}
        </div>
      </div>
    </div>
  `;
}
