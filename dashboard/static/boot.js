/* Boot — clock, nav, live polling */

function tickClock() {
  const el = document.getElementById('clock');
  if (el) el.textContent = new Date().toLocaleTimeString();
}

async function refreshHealth() {
  const h = await api('/api/health');
  const dot = document.querySelector('#system-status .status-dot');
  const text = document.getElementById('status-text');
  if (!h) {
    if (dot) dot.className = 'status-dot red';
    if (text) text.textContent = 'Unreachable';
    return;
  }
  const ok = h.status === 'healthy' || h.status === 'ok';
  if (dot) dot.className = `status-dot ${ok ? 'green' : 'yellow'}`;
  if (text) {
    const n = h.agents_online != null ? h.agents_online : Object.values(h.agents_running || {}).filter(Boolean).length;
    text.textContent = ok ? `Nominal · ${n} crew` : 'Degraded';
  }
}

async function refreshAgentsSilent() {
  const res = await api('/api/agents');
  if (!res) return;
  const list = res.agents || [];
  S.agents = Array.isArray(list) ? list : Object.values(list);
  S.agentsMap = res.agents_map || {};
  if (!Object.keys(S.agentsMap).length) {
    S.agents.forEach(a => { S.agentsMap[a.name] = a; });
  }
  renderSidebar();
}

async function refreshActiveView() {
  const view = S.currentView;
  const target = document.getElementById(`${view}-content`);
  if (!target) return;
  // don't interrupt chat mid-stream
  if (view === 'chat') return;
  try {
    if (view === 'dashboard') await renderDashboard(target);
    else if (view === 'agents') await renderAgentView(target);
    else if (view === 'fleet') await renderFleetView(target);
    else if (view === 'incidents') await renderIncidentsView(target);
    else if (view === 'connect') await renderConnectView(target);
    else if (view === 'shield') await renderShieldView(target);
  } catch (e) {
    console.error('refresh view', view, e);
  }
}

document.addEventListener('DOMContentLoaded', async () => {
  tickClock();
  setInterval(tickClock, 1000);

  // Nav first so tabs always work
  document.querySelectorAll('.nav-item').forEach(item => {
    item.addEventListener('click', (e) => {
      e.preventDefault();
      const view = item.dataset.view;
      if (!view) return;
      try { renderView(view); }
      catch (err) {
        console.error(err);
        showToast(`View error: ${err.message || err}`, 4000, 'error');
      }
    });
  });

  document.getElementById('right-panel-close')?.addEventListener('click', clearRightPanel);

  await refreshHealth();
  await refreshAgentsSilent();

  try {
    renderView('dashboard');
  } catch (err) {
    console.error(err);
    const area = document.getElementById('dashboard-content');
    if (area) showError(area, err.message || String(err));
  }

  // Live polling: health 5s, agents 5s, active view 3s
  setInterval(refreshHealth, 5000);
  setInterval(refreshAgentsSilent, 5000);
  S.polling = setInterval(refreshActiveView, 3000);
});
