/* Incidents — live or empty */

async function renderIncidentsView(area) {
  showLoading(area);
  const res = await api('/api/incidents');
  const list = (res && res.incidents) || [];
  S.incidents = list;

  if (!list.length) {
    showNoData(area, 'No active incidents', 'Incident feed is quiet. All clear.');
    return;
  }

  area.innerHTML = `
    <div class="view-header">
      <h2><span>Incidents</span></h2>
      <span class="muted">${list.length} open</span>
    </div>
    <div class="section glass" style="padding:0 0 8px">
      <table>
        <tr><th>ID</th><th>Severity</th><th>Title</th><th>Status</th></tr>
        ${list.map(i => `<tr>
          <td class="mono">${escapeHtml(i.id || '')}</td>
          <td><span class="badge ${i.severity === 'critical' ? 'danger' : i.severity === 'high' ? 'warn' : ''}">${escapeHtml(i.severity || '—')}</span></td>
          <td>${escapeHtml(i.title || i.summary || '')}</td>
          <td>${escapeHtml(i.status || '')}</td>
        </tr>`).join('')}
      </table>
    </div>
  `;
}
