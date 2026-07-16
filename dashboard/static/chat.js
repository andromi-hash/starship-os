/* Agent Officer Check-In — Ollama SSE + NATS */

function renderChatView(area) {
  const agents = S.agents.length ? S.agents : [
    { name: 'proxy' }, { name: 'romi' }, { name: 'ergo' },
  ];
  if (!S.chatAgent) S.chatAgent = agents[0]?.name || 'proxy';

  area.innerHTML = `
    <div class="view-header">
      <h2><span>Officer Check-In</span></h2>
      <span class="muted" style="font-size:12px">Ollama stream · NATS dispatch</span>
    </div>
    <div class="chat-layout">
      <div class="chat-toolbar">
        <label class="muted" style="font-size:12px">Agent</label>
        <select id="chat-agent-select" onchange="S.chatAgent=this.value">
          ${agents.map(a => `<option value="${escapeHtml(a.name)}" ${a.name === S.chatAgent ? 'selected' : ''}>${escapeHtml(a.name)}</option>`).join('')}
        </select>
      </div>
      <div class="quick-actions">
        <button class="btn btn-secondary" onclick="quickAction('ergo','status')">Ergo Status</button>
        <button class="btn btn-secondary" onclick="quickAction('romi','check')">Romi Check</button>
        <button class="btn btn-secondary" onclick="quickAction('proxy','ping')">Proxy Ping</button>
        <button class="btn btn-secondary" onclick="quickAction('ergo','health check')">Health Check</button>
        <button class="btn btn-secondary" onclick="quickAction('proxy','security audit')">Security Audit</button>
      </div>
      <div class="chat-messages glass" id="chat-messages">
        <div class="msg agent">
          <div class="sender">BRIDGE</div>
          <div>Welcome, Commander. Select an officer and send a command. Responses stream live from Ollama.</div>
        </div>
      </div>
      <div class="chat-input-row">
        <input id="chat-input" type="text" placeholder="Enter command or message…" onkeydown="if(event.key==='Enter')sendChat()">
        <button class="btn btn-primary" id="chat-send-btn" onclick="sendChat()">Send</button>
      </div>
    </div>
  `;
}

function appendMsg(cls, sender, text) {
  const box = document.getElementById('chat-messages');
  if (!box) return null;
  const div = document.createElement('div');
  div.className = `msg ${cls}`;
  div.innerHTML = `${sender ? `<div class="sender">${escapeHtml(sender)}</div>` : ''}<div class="msg-body"></div>`;
  div.querySelector('.msg-body').textContent = text || '';
  box.appendChild(div);
  box.scrollTop = box.scrollHeight;
  return div;
}

function quickAction(agent, command) {
  S.chatAgent = agent;
  const sel = document.getElementById('chat-agent-select');
  if (sel) sel.value = agent;
  const input = document.getElementById('chat-input');
  if (input) input.value = command;
  sendChat();
}

async function sendChat() {
  const input = document.getElementById('chat-input');
  const btn = document.getElementById('chat-send-btn');
  if (!input) return;
  const command = input.value.trim();
  if (!command) return;
  const agent = S.chatAgent || 'proxy';
  input.value = '';
  if (btn) btn.disabled = true;

  appendMsg('user', 'YOU', command);
  const agentMsg = appendMsg('agent', agent.toUpperCase(), '');
  const bodyEl = agentMsg?.querySelector('.msg-body');
  let full = '';

  try {
    const res = await fetch('/api/chat/stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ agent, command, args: {} }),
    });
    if (!res.ok || !res.body) {
      if (bodyEl) bodyEl.textContent = `Error: HTTP ${res.status}`;
      showToast('Chat stream failed', 3000, 'error');
      return;
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = '';

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      const parts = buf.split('\n\n');
      buf = parts.pop() || '';
      for (const part of parts) {
        const lines = part.split('\n');
        let event = 'message';
        let data = '';
        for (const line of lines) {
          if (line.startsWith('event:')) event = line.slice(6).trim();
          else if (line.startsWith('data:')) data += line.slice(5).trim();
        }
        if (!data) continue;
        let payload;
        try { payload = JSON.parse(data); } catch { continue; }

        if (event === 'token') {
          full += payload.text || '';
          if (bodyEl) bodyEl.textContent = full;
        } else if (event === 'tool_start') {
          appendMsg('tool', 'TOOL', `▶ ${payload.tool} ${JSON.stringify(payload.args || {})}`);
        } else if (event === 'tool_complete') {
          appendMsg('tool', 'TOOL', `✓ ${payload.tool}: ${payload.summary || ''}`);
        } else if (event === 'response') {
          full = payload.text || full;
          if (bodyEl) bodyEl.textContent = full;
        } else if (event === 'error') {
          if (bodyEl) bodyEl.textContent = (full ? full + '\n' : '') + `Error: ${payload.error || 'unknown'}`;
          showToast(payload.error || 'Chat error', 4000, 'error');
        } else if (event === 'step') {
          // optional step indicator
        }
        const box = document.getElementById('chat-messages');
        if (box) box.scrollTop = box.scrollHeight;
      }
    }
    if (!full && bodyEl && !bodyEl.textContent) {
      bodyEl.textContent = '(no response)';
    }
  } catch (e) {
    if (bodyEl) bodyEl.textContent = `Error: ${e.message || e}`;
    showToast(String(e.message || e), 4000, 'error');
  } finally {
    if (btn) btn.disabled = false;
  }
}
