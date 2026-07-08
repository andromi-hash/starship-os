#!/usr/bin/env python3
"""Starship OS System Tray Indicator — agent status & quick commands."""

import sys
import os
import json
import threading
import subprocess
from pathlib import Path
from datetime import datetime

import gi
gi.require_version('Gtk', '3.0')
try:
    gi.require_version('AppIndicator3', '0.1')
    from gi.repository import AppIndicator3 as Indicator
except ValueError:
    gi.require_version('AyatanaAppIndicator3', '0.1')
    from gi.repository import AyatanaAppIndicator3 as Indicator
from gi.repository import Gtk, GLib

STATUS_FILE = Path("/tmp/starship-status.json")
PROJECT_DIR = Path(os.getenv("STARSHIP_ROOT", os.path.dirname(os.path.abspath(__file__))).replace("/tray", ""))
AGENTS = ["proxy", "romi", "ergo"]

ICONS = {
    "online": "●",
    "processing": "◉",
    "offline": "○",
    "unknown": "?",
}
COLORS = {
    "online": "#81c784",
    "processing": "#ffb74d",
    "offline": "#e57373",
    "unknown": "#a0a0a0",
}


class StarshipIndicator:
    def __init__(self):
        self.indicator = Indicator.Indicator.new(
            "starship-indicator",
            "utilities-system-monitor",
            AppIndicator3.IndicatorCategory.APPLICATION_STATUS,
        )
        self.indicator.set_status(AppIndicator3.IndicatorStatus.ACTIVE)
        self.menu = Gtk.Menu()
        self.refresh_menu()
        self.indicator.set_menu(self.menu)
        GLib.timeout_add_seconds(3, self.refresh_menu)

    def get_status(self):
        try:
            return json.loads(STATUS_FILE.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return {"agents": {}, "telemetry": {}}

    def refresh_menu(self):
        for w in self.menu.get_children():
            self.menu.remove(w)

        status = self.get_status()
        agents = status.get("agents", {})
        telemetry = status.get("telemetry", {}).get("full", {})

        # Header
        item = Gtk.MenuItem(label="⭐ Starship OS Agents")
        item.set_sensitive(False)
        item.show()
        self.menu.append(item)
        self.menu.append(Gtk.SeparatorMenuItem())

        # Agent status items
        for name in AGENTS:
            a = agents.get(name, {})
            s = a.get("status", "unknown")
            icon = ICONS.get(s, "?")
            color = COLORS.get(s, "#a0a0a0")
            label = f"  {icon}  {name.capitalize()} — {s}"
            item = Gtk.MenuItem(label=label)
            item.connect("activate", self.on_agent_click, name)
            item.show()
            self.menu.append(item)

        self.menu.append(Gtk.SeparatorMenuItem())

        # Telemetry
        if telemetry:
            cpu = telemetry.get("cpu", "?")
            mu = telemetry.get("memory_used", 0) // (1024**3)
            mt = telemetry.get("memory_total", 0) // (1024**3)
            item = Gtk.MenuItem(label=f"  CPU: {cpu}%  RAM: {mu}/{mt}GB")
            item.set_sensitive(False)
            item.show()
            self.menu.append(item)
            self.menu.append(Gtk.SeparatorMenuItem())

        # Agent chat actions
        for name in AGENTS:
            item = Gtk.MenuItem(label=f"  💬 Chat with {name.capitalize()}")
            item.connect("activate", self.on_chat_click, name)
            item.show()
            self.menu.append(item)

        self.menu.append(Gtk.SeparatorMenuItem())

        # Quick commands
        item = Gtk.MenuItem(label="  📊 Open dashboard")
        item.connect("activate", self.on_dashboard_click)
        item.show()
        self.menu.append(item)

        item = Gtk.MenuItem(label="  🛑 Stop all agents")
        item.connect("activate", self.on_stop_click)
        item.show()
        self.menu.append(item)

        self.menu.append(Gtk.SeparatorMenuItem())
        item = Gtk.MenuItem(label="  Quit")
        item.connect("activate", self.on_quit)
        item.show()
        self.menu.append(item)

        return True

    def on_agent_click(self, widget, name):
        os.system(f"x-terminal-emulator -e 'starship agent status {name}; read -p \"Press enter...\"' &")

    def on_chat_click(self, widget, name):
        os.system(f"x-terminal-emulator -e 'starship agent chat {name}' &")

    def on_dashboard_click(self, widget):
        os.system("xdg-open http://localhost:8899 &")

    def on_stop_click(self, widget):
        subprocess.Popen([str(PROJECT_DIR / "agents" / "run_agent.sh"), "stop"])

    def on_quit(self, widget):
        Gtk.main_quit()

    def run(self):
        Gtk.main()


if __name__ == "__main__":
    StarshipIndicator().run()
