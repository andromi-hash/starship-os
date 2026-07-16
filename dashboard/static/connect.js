/* Connect — remote agent communication gateway */

async function renderConnectView(area) {
  showLoading(area);

  const [health, agents, info] = await Promise.all([
    api('/api/health'),
    api('/api/agents'),
    api('/api/agent/installer-info'),
  ]);

  const natsUrl = (info && info.nats_url) || 'nats://hub:4222';
  const subjectBase = 'starship.agent';
  const agentList = (agents && agents.agents) || [];
  const agentNames = Array.isArray(agentList) ? agentList.map(a => a.name || a) : Object.keys(agentList);

  area.innerHTML = `
    <div class="view-header">
      <h2><span>Connect</span> · Remote Agent Gateway</h2>
      <span class="muted mono" style="font-size:11px">${new Date().toLocaleTimeString()}</span>
    </div>

    <div class="section glass">
      <div class="panel-title">Remote Conversations via Simplex Messenger</div>
      <div style="padding:0 14px 14px">
        <p style="margin:0 0 10px;color:var(--color-text-muted);font-size:13px">
          Simplex Messenger is a privacy-preserving messaging platform that can act as a gateway
          to relay messages to and from your Starship OS agents. This lets you check in with your
          crew from anywhere using your phone or desktop.
        </p>

        <div class="plant-card glass" style="margin-bottom:10px">
          <div class="plant-name">How It Works</div>
          <div class="plant-meta" style="flex-direction:column;align-items:flex-start;gap:4px">
            <span>1. Install Simplex Messenger on your device</span>
            <span>2. A bridge bot runs on the hub, subscribing to agent NATS subjects</span>
            <span>3. Send messages to the bridge → it relays to the target agent</span>
            <span>4. Agent responses are relayed back to your Simplex chat</span>
          </div>
        </div>

        <div class="plant-card glass" style="margin-bottom:10px">
          <div class="plant-name">Active Agents</div>
          <div class="plant-meta" style="flex-wrap:wrap;gap:6px">
            ${agentNames.length ? agentNames.map(name =>
              `<span class="badge">${escapeHtml(name)}</span>`
            ).join('') : '<span class="muted">No agents registered</span>'}
          </div>
        </div>

        <div class="plant-card glass" style="margin-bottom:10px">
          <div class="plant-name">NATS Bridge Subjects</div>
          <div class="plant-meta" style="flex-direction:column;align-items:flex-start;gap:4px">
            <code class="mono" style="font-size:12px">${escapeHtml(subjectBase)}.{agent}.command</code>
            <code class="mono" style="font-size:12px">${escapeHtml(subjectBase)}.{agent}.event</code>
            <code class="mono" style="font-size:12px">${escapeHtml(subjectBase)}.{agent}.status</code>
          </div>
        </div>

        <div style="margin-top:12px">
          <div style="font-size:11px;color:var(--color-text-muted);margin-bottom:6px">Simplex Bridge Configuration</div>
          <div style="background:var(--color-glass);padding:12px;border-radius:6px;border:1px solid var(--color-glass-edge)">
            <pre class="mono" style="margin:0;font-size:11px;line-height:1.6;white-space:pre-wrap"># Example bridge config (simplex-bridge.yaml)
nats:
  url: "${escapeHtml(natsUrl)}"
  subjects:
    command: "${escapeHtml(subjectBase)}.{agent}.command"
    event:   "${escapeHtml(subjectBase)}.{agent}.event"
    status:  "${escapeHtml(subjectBase)}.{agent}.status"

agents:
${agentNames.length ? agentNames.map(name => `  - name: ${escapeHtml(name)}`).join('\n') : '  # no agents detected'}

simplex:
  bot_name: "Starship Bridge"
  passphrase: "your-simplex-passphrase"
  display_name: "Starship OS"
</pre>
          </div>
        </div>
      </div>
    </div>

    <div class="section glass" style="margin-top:16px">
      <div class="panel-title">Web Gateway <span class="badge" style="font-size:10px">Coming Soon</span></div>
      <div style="padding:0 14px 14px">
        <div class="empty-state" style="padding:16px">
          <div class="icon" style="font-size:28px">🌐</div>
          <h3>Direct Web Agent Communication</h3>
          <p style="max-width:480px">
            A web-based gateway is in development that will let you connect directly to
            agents through this dashboard — no third-party apps required. You'll be able
            to select an agent, start a conversation, and receive real-time responses
            right in your browser.
          </p>
        </div>
      </div>
    </div>
  `;
}
