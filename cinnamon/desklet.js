const St = imports.gi.St;
const Desklet = imports.ui.desklet;
const Mainloop = imports.mainloop;
const Gio = imports.gi.Gio;
const Settings = imports.ui.settings;

const AGENT_NAMES = ['ergo', 'romi', 'proxy'];

function statusClass(status) {
    const s = (status || '').toLowerCase();
    if (s === 'online' || s === 'complete') return 'online';
    if (s === 'processing' || s === 'busy') return 'busy';
    if (s === 'warning') return 'warning';
    return 'offline';
}

const StarshipOSDesklet = class extends Desklet.Desklet {
    constructor(metadata, desklet_id) {
        super(metadata, desklet_id);
        this._updateId = 0;

        this.settings = new Settings.DeskletSettings(this, metadata.uuid, this.instance_id);
        this.settings.bind('refresh-interval', 'refreshInterval', this._onSettingsChanged.bind(this));

        this._buildUI();
        this._scheduleUpdate();
    }

    _buildUI() {
        this._main = new St.BoxLayout({ vertical: true, style_class: 'starship-container' });

        const hdr = new St.BoxLayout({ style_class: 'starship-header' });
        hdr.add_child(new St.Label({ text: 'STARSHIP OS', style_class: 'starship-title' }));
        this._statusDot = new St.Label({ text: '●', style_class: 'starship-status-global' });
        hdr.add_child(this._statusDot);
        this._main.add_child(hdr);

        this._main.add_child(new St.Label({ text: 'AGENT STATUS', style_class: 'section-title' }));
        this._agentsBox = new St.BoxLayout({ vertical: true, style_class: 'agents-container' });
        this._agentRows = {};
        for (const name of AGENT_NAMES) {
            const row = new St.BoxLayout({ style_class: 'agent-row' });
            const dot = new St.Bin({ style_class: 'status-dot offline' });
            const label = new St.Label({ text: name.charAt(0).toUpperCase() + name.slice(1), style_class: 'agent-name' });
            const status = new St.Label({ text: 'OFFLINE', style_class: 'agent-status offline' });
            row.add_child(dot);
            row.add_child(label);
            row.add_child(status);
            this._agentsBox.add_child(row);
            this._agentRows[name] = { dot, status };
        }
        this._main.add_child(this._agentsBox);

        this._main.add_child(new St.Label({ text: 'SYSTEM TELEMETRY', style_class: 'section-title' }));
        this._telemetryGrid = new St.BoxLayout({ style_class: 'telemetry-grid' });
        this._telemetryCells = {};
        for (const key of ['cpu', 'mem', 'disk']) {
            const cell = new St.BoxLayout({ vertical: true, style_class: 'telemetry-cell' });
            const val = new St.Label({ text: '--', style_class: 'telemetry-value' });
            const lbl = new St.Label({ text: key.toUpperCase(), style_class: 'telemetry-label' });
            cell.add_child(val);
            cell.add_child(lbl);
            this._telemetryGrid.add_child(cell);
            this._telemetryCells[key] = val;
        }
        this._main.add_child(this._telemetryGrid);

        this._main.add_child(new St.Label({ text: 'LATEST COMM', style_class: 'section-title' }));
        this._commLabel = new St.Label({ text: 'Awaiting transmission...', style_class: 'comm-message' });
        this._main.add_child(this._commLabel);

        this.setContent(this._main);
    }

    _onSettingsChanged() {
        if (this._updateId) {
            Mainloop.source_remove(this._updateId);
        }
        this._scheduleUpdate();
    }

    _scheduleUpdate() {
        this._loadData();
        this._updateId = Mainloop.timeout_add_seconds(this.refreshInterval || 5, () => {
            this._loadData();
            return true;
        });
    }

    _loadData() {
        try {
            const file = Gio.file_new_for_path('/tmp/starship-status.json');
            const [success, contents] = file.load_contents(null);
            if (success) {
                const data = JSON.parse(contents);
                this._render(data);
            }
        } catch (e) {
            global.logError('StarshipOS desklet: ' + String(e));
        }
    }

    _render(data) {
        const agents = data.agents || {};
        const telemetry = data.telemetry || {};
        const messages = data.messages || [];

        // Agents
        let allOnline = true;
        for (const name of AGENT_NAMES) {
            const agent = agents[name] || {};
            const cls = statusClass(agent.status);
            const row = this._agentRows[name];
            if (row) {
                row.dot.style_class = 'status-dot ' + cls;
                row.status.text = (agent.status || 'offline').toUpperCase();
                row.status.style_class = 'agent-status ' + cls;
            }
            if (cls !== 'online') allOnline = false;
        }
        this._statusDot.style_class = 'starship-status-global';
        this._statusDot.text = allOnline ? '●' : '◉';

        // Telemetry
        const t = telemetry.full || {};
        const setT = (key, val) => {
            if (this._telemetryCells[key]) this._telemetryCells[key].text = val != null ? String(val) : '--';
        };
        setT('cpu', t.cpu != null ? t.cpu.toFixed(1) + '%' : '--');
        if (t.memory_used != null && t.memory_total != null) {
            const mu = Math.round(t.memory_used / 1073741824);
            const mt = Math.round(t.memory_total / 1073741824);
            setT('mem', mu + '/' + mt + 'GB');
        } else {
            setT('mem', '--');
        }
        if (t.disk_used != null && t.disk_total != null) {
            const du = Math.round(t.disk_used / 1073741824 / 1024);
            const dt = Math.round(t.disk_total / 1073741824 / 1024);
            setT('disk', du + '/' + dt + 'TB');
        } else {
            setT('disk', '--');
        }

        // Latest message
        if (messages.length > 0) {
            const last = messages[0];
            const agent = last.agent || '?';
            const cls = AGENT_NAMES.indexOf(agent) !== -1 ? agent : 'proxy';
            const text = (last.response || '').substring(0, 120);
            this._commLabel.style_class = 'comm-message ' + cls;
            this._commLabel.text = text || 'No transmission content';
        }
    }

    on_desklet_removed() {
        if (this._updateId) {
            Mainloop.source_remove(this._updateId);
        }
    }
};

function main(metadata, desklet_id) {
    return new StarshipOSDesklet(metadata, desklet_id);
}
