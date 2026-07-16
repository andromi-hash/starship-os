/* ── Boot & Initialization ─────────────────────────────────────────────── */

document.addEventListener('DOMContentLoaded', async () => {
  const [healthResult, agentsResult, incidentsResult] = await Promise.all([
    api('/api/health'),
    api('/api/agents'),
    api('/api/incidents'),
  ]);

  if (healthResult) {
    const statusDot = document.querySelector('#system-status .status-dot');
    const statusText = document.getElementById('status-text');
    if (healthResult.status === 'healthy') {
      statusDot.className = 'status-dot green';
      statusText.textContent = 'All Systems Nominal';
    } else {
      statusDot.className = 'status-dot yellow';
      statusText.textContent = 'Degraded';
    }
  }

  S.agents = agentsResult?.agents || [];
  S.incidents = incidentsResult?.incidents || [];

  // ── Navigation first so a render error never leaves tabs dead ──────────
  document.querySelectorAll('.nav-item').forEach(item => {
    item.addEventListener('click', (e) => {
      e.preventDefault();
      const view = item.dataset.view;
      if (!view) return;
      try {
        renderView(view);
      } catch (err) {
        console.error('renderView failed', view, err);
        showToast(`View error: ${err.message || err}`, 4000, 'error');
      }
    });
  });

  try {
    renderSidebar();
  } catch (err) {
    console.error('renderSidebar failed', err);
  }
  try {
    renderView('dashboard');
  } catch (err) {
    console.error('initial dashboard render failed', err);
    const area = document.getElementById('dashboard-content');
    if (area) showError(area, err.message || String(err));
  }

  // ── Right panel close ──────────────────────────────────────────────────
  document.getElementById('right-panel-close')?.addEventListener('click', clearRightPanel);

  // ── Agent polling ──────────────────────────────────────────────────────
  S.polling = setInterval(async () => {
    const result = await api('/api/agents');
    if (result && result.agents) {
      S.agents = result.agents;
      renderSidebar();
    }
  }, 30000);

  // ── Keyboard shortcuts ─────────────────────────────────────────────────
  document.addEventListener('keydown', (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === 'k') {
      e.preventDefault();
      showCommandPalette();
    }
    if ((e.ctrlKey || e.metaKey) && e.key === 'n') {
      e.preventDefault();
      renderView('chat');
    }
    if (e.key === 'Escape') {
      if (S.rightPanelContent) clearRightPanel();
    }
  });
});

// ── Command Palette ──────────────────────────────────────────────────────

function showCommandPalette() {
  const view = S.currentView;
  const commands = [
    { icon: '&#9632;', label: 'Go to Dashboard', action: () => renderView('dashboard') },
    { icon: '&#9679;', label: 'Go to Agents', action: () => renderView('agents') },
    { icon: '&#9993;', label: 'Go to Chat', action: () => renderView('chat') },
    { icon: '&#9888;', label: 'Go to Incidents', action: () => renderView('incidents') },
    { icon: '&#9878;', label: 'Go to Policy', action: () => renderView('policy') },
    { icon: '&#8226;', label: 'Go to Memory', action: () => renderView('memory') },
    { icon: '&#9733;', label: 'Go to Shield', action: () => renderView('shield') },
    { icon: '&#9733;', label: 'Go to Skills', action: () => renderView('skills') },
    { icon: '&#9772;', label: 'Go to Telemetry', action: () => renderView('telemetry') },
    { icon: '&#9783;', label: 'Go to Accounts', action: () => renderView('accounts') },
    { icon: '&#9993;', label: 'Go to Email', action: () => renderView('email') },
    { icon: '&#9632;', label: 'Go to Org Chart', action: () => renderView('orgchart') },
    { icon: '&#9733;', label: 'Go to Goals', action: () => renderView('goals') },
    { icon: '+', label: 'New Chat Session', action: () => renderView('chat') },
  ];

  const overlay = document.createElement('div');
  overlay.className = 'modal-overlay';
  overlay.style.alignItems = 'flex-start';
  overlay.style.paddingTop = '120px';
  overlay.innerHTML = `
    <div class="modal" style="min-width:480px;">
      <input type="text" id="palette-input" placeholder="Type a command..." autofocus
        style="background:rgba(255,255,255,0.06);border:1px solid rgba(255,255,255,0.1);border-radius:8px;padding:12px 16px;color:#fff;font-size:15px;width:100%;margin-bottom:12px;">
      <div id="palette-results" style="max-height:300px;overflow-y:auto;"></div>
      <div style="font-size:11px;color:rgba(255,255,255,0.3);margin-top:8px;">
        <kbd>&uarr;</kbd> <kbd>&darr;</kbd> navigate &middot; <kbd>Enter</kbd> select &middot; <kbd>Esc</kbd> close
      </div>
    </div>`;
  document.body.appendChild(overlay);

  const input = overlay.querySelector('#palette-input');
  const results = overlay.querySelector('#palette-results');
  let selectedIdx = 0;

  function render(filter) {
    const filtered = filter
      ? commands.filter(c => c.label.toLowerCase().includes(filter.toLowerCase()))
      : commands;
    selectedIdx = 0;
    results.innerHTML = filtered.map((c, i) =>
      `<div class="palette-item ${i === 0 ? 'selected' : ''}" data-idx="${i}"
        style="padding:8px 12px;border-radius:6px;cursor:pointer;display:flex;align-items:center;gap:8px;color:rgba(255,255,255,0.7);transition:all 0.1s;${i === 0 ? 'background:rgba(139,123,214,0.15);color:#fff;' : ''}">
        <span style="width:20px;text-align:center;">${c.icon}</span>
        ${escapeHtml(c.label)}
      </div>`
    ).join('');
    results.querySelectorAll('.palette-item').forEach(el => {
      el.addEventListener('click', () => {
        const idx = parseInt(el.dataset.idx);
        filtered[idx].action();
        overlay.remove();
      });
      el.addEventListener('mouseenter', () => {
        results.querySelectorAll('.palette-item').forEach(e => {
          e.style.background = '';
          e.style.color = 'rgba(255,255,255,0.7)';
        });
        el.style.background = 'rgba(139,123,214,0.15)';
        el.style.color = '#fff';
        selectedIdx = parseInt(el.dataset.idx);
      });
    });
  }

  render('');

  input.addEventListener('input', () => render(input.value));

  input.addEventListener('keydown', (e) => {
    const items = results.querySelectorAll('.palette-item');
    if (e.key === 'ArrowDown') { e.preventDefault(); selectedIdx = Math.min(selectedIdx + 1, items.length - 1); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); selectedIdx = Math.max(selectedIdx - 1, 0); }
    else if (e.key === 'Enter') {
      e.preventDefault();
      const filter = input.value;
      const filtered = filter ? commands.filter(c => c.label.toLowerCase().includes(filter.toLowerCase())) : commands;
      if (filtered[selectedIdx]) { filtered[selectedIdx].action(); overlay.remove(); }
      return;
    }
    items.forEach((el, i) => {
      el.style.background = i === selectedIdx ? 'rgba(139,123,214,0.15)' : '';
      el.style.color = i === selectedIdx ? '#fff' : 'rgba(255,255,255,0.7)';
    });
  });

  input.focus();

  // Click outside to close
  overlay.addEventListener('click', (e) => {
    if (e.target === overlay) overlay.remove();
  });
}
