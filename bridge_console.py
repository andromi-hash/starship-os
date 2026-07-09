#!/usr/bin/env python3
"""Starship OS — Bridge Console (GTK4 Native App)
Reads /tmp/starship-status.json, live-updates via inotify.
Matches Starship-OS theme CSS classes.
"""

import json
import os
import sys
import time
import signal
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("GLib", "2.0")
from gi.repository import Gtk, GLib, Gio, Gdk, Pango

STATUS_PATH = "/tmp/starship-status.json"
POLL_MS = 2000

CSS = """
window {
  background: #070B14;
  color: #E4DDD0;
}
.title-label {
  font-family: "Inter", sans-serif;
  font-size: 17px;
  font-weight: 500;
  color: #7BC8E4;
  letter-spacing: 0.04em;
}
.subtitle {
  font-size: 10px;
  color: #5A6A80;
  letter-spacing: 0.06em;
  font-weight: 400;
}
.panel {
  background: rgba(14, 22, 40, 0.55);
  border: 0.5px solid rgba(60, 80, 120, 0.15);
  border-radius: 10px;
  padding: 14px;
}
.panel-title {
  font-size: 9px;
  font-weight: 500;
  color: #5A6A80;
  text-transform: uppercase;
  letter-spacing: 0.1em;
  margin-bottom: 6px;
}
.agent-name {
  font-weight: 500;
  color: #E4DDD0;
  font-size: 12px;
}
.agent-online { color: #7BC8A4; }
.agent-busy { color: #7BC8E4; }
.agent-warning { color: #D4A060; }
.agent-offline { color: #D46060; }
.telemetry-value {
  font-family: "JetBrains Mono", "Fira Code", monospace;
  font-size: 16px;
  font-weight: 500;
  color: #7BC8E4;
}
.telemetry-label {
  font-size: 8px;
  font-weight: 500;
  color: #5A6A80;
  text-transform: uppercase;
  letter-spacing: 0.08em;
}
.mono {
  font-family: "JetBrains Mono", "Fira Code", monospace;
  font-size: 11px;
  color: #8A9BB0;
}
"""


def load_status():
    try:
        with open(STATUS_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


class StarshipBridgeApp(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="com.starship-os.bridge-console")
        self.window = None
        self.status = None
        self.timer_id = None

    def do_activate(self):
        if self.window:
            self.window.present()
            return

        win = Gtk.ApplicationWindow(application=self)
        win.set_title("Starship OS — Bridge Console")
        win.set_default_size(640, 480)
        win.set_resizable(True)

        css_provider = Gtk.CssProvider()
        css_provider.load_from_string(CSS)
        Gtk.StyleContext.add_provider_for_display(
            win.get_display(), css_provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        vbox.set_margin_start(16)
        vbox.set_margin_end(16)
        vbox.set_margin_top(16)
        vbox.set_margin_bottom(16)

        # Header
        header = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        title = Gtk.Label(label="STARSHIP OS")
        title.add_css_class("title-label")
        subtitle = Gtk.Label(label="BRIDGE CONSOLE  ·  COMMONWEALTH ANDROMEDA / HIGHGUARD")
        subtitle.add_css_class("subtitle")
        header.append(title)
        header.append(subtitle)

        # Panes
        paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        paned.set_position(240)

        # Left: agents + telemetry
        left_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)

        # Agents panel
        agents_frame = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        agents_frame.add_css_class("panel")
        agents_title = Gtk.Label(label="AGENTS")
        agents_title.add_css_class("panel-title")
        agents_title.set_halign(Gtk.Align.START)
        agents_frame.append(agents_title)

        self.agent_grid = Gtk.Grid()
        self.agent_grid.set_column_spacing(8)
        self.agent_grid.set_row_spacing(6)
        agents_frame.append(self.agent_grid)

        left_box.append(agents_frame)

        # Telemetry panel
        tele_frame = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        tele_frame.add_css_class("panel")
        tele_title = Gtk.Label(label="TELEMETRY")
        tele_title.add_css_class("panel-title")
        tele_title.set_halign(Gtk.Align.START)
        tele_frame.append(tele_title)

        self.tele_grid = Gtk.Grid()
        self.tele_grid.set_column_spacing(12)
        self.tele_grid.set_row_spacing(8)
        self.tele_grid.set_halign(Gtk.Align.CENTER)
        tele_frame.append(self.tele_grid)

        left_box.append(tele_frame)

        # Right: comm log
        right_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        right_box.add_css_class("panel")

        comm_title = Gtk.Label(label="COMM LOG")
        comm_title.add_css_class("panel-title")
        comm_title.set_halign(Gtk.Align.START)
        right_box.append(comm_title)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)

        self.comm_text = Gtk.Label(label="Awaiting agent communications...")
        self.comm_text.add_css_class("mono")
        self.comm_text.set_xalign(0)
        self.comm_text.set_wrap(True)
        self.comm_text.set_selectable(True)
        scrolled.set_child(self.comm_text)

        right_box.append(scrolled)

        # Assemble paned
        paned.set_start_child(left_box)
        paned.set_end_child(right_box)

        vbox.append(header)
        vbox.append(paned)

        win.set_child(vbox)
        self.window = win
        win.present()

        # Start polling
        self.timer_id = GLib.timeout_add(POLL_MS, self.poll_status)
        self.poll_status()

    def poll_status(self):
        data = load_status()
        if data and data != self.status:
            self.status = data
            self.update_ui()
        return True

    def update_ui(self):
        self.update_agents()
        self.update_telemetry()
        self.update_comm()

    def update_agents(self):
        while child := self.agent_grid.get_first_child():
            self.agent_grid.remove(child)

        agents = self.status.get("agents", {})
        if not agents:
            return

        row = 0
        for name, info in agents.items():
            status = info.get("status", "offline")
            dot = Gtk.Label(label="●")
            sclass = f"agent-{status}"
            dot.add_css_class(sclass)
            dot.set_margin_end(4)

            nlabel = Gtk.Label(label=name.capitalize())
            nlabel.add_css_class("agent-name")

            slabel = Gtk.Label(label=status.upper())
            slabel.add_css_class(sclass)
            slabel.add_css_class("telemetry-label")

            self.agent_grid.attach(dot, 0, row, 1, 1)
            self.agent_grid.attach(nlabel, 1, row, 1, 1)
            self.agent_grid.attach(slabel, 2, row, 1, 1)
            row += 1

    def update_telemetry(self):
        while child := self.tele_grid.get_first_child():
            self.tele_grid.remove(child)

        tele = self.status.get("telemetry", {})

        def add_item(label_text, key, row, col):
            val = tele.get(key, "—")
            if isinstance(val, float):
                val = f"{val:.1f}"

            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            v = Gtk.Label(label=str(val))
            v.add_css_class("telemetry-value")
            l = Gtk.Label(label=label_text)
            l.add_css_class("telemetry-label")
            box.append(v)
            box.append(l)
            self.tele_grid.attach(box, col, row, 1, 1)

        add_item("CPU %", "cpu_percent", 0, 0)
        add_item("RAM", "memory_percent", 0, 1)
        add_item("↑ KB/s", "net_sent", 0, 2)
        add_item("↓ KB/s", "net_recv", 0, 3)

        add_item("Load", "load_1m", 1, 0)
        add_item("Procs", "process_count", 1, 1)
        add_item("Disk %", "disk_percent", 1, 2)
        add_item("Uptime h", "uptime_hours", 1, 3)

    def update_comm(self):
        comm = self.status.get("comm", {})
        parts = []
        for agent_name, msg in comm.items():
            if isinstance(msg, str) and len(msg) > 10:
                preview = msg[:200].replace("\n", " ")
                parts.append(f"[{agent_name.upper()}] {preview}…")
        if parts:
            self.comm_text.set_label("\n\n".join(parts))
        else:
            self.comm_text.set_label("No recent communications.")


def main():
    app = StarshipBridgeApp()
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    exit_status = app.run(sys.argv)
    sys.exit(exit_status)


if __name__ == "__main__":
    main()
