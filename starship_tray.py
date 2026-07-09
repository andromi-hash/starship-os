#!/usr/bin/env python3
"""Starship OS — System Tray Indicator
GTK3 StatusIcon that shows agent status and provides quick-access menu.
"""

import json
import os
import signal
import subprocess
import sys
import threading
import time

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("GLib", "2.0")
gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")
import cairo
from gi.repository import Gtk, GLib, Gdk

STATUS_PATH = "/tmp/starship-status.json"
POLL_INTERVAL = 3
DASHBOARD_URL = "http://localhost:8899"
DASHBOARD_SERVER = "/home/tech/starship-os/dashboard/server.py"
HERMES_PYTHON = "/home/tech/.hermes/hermes-agent/venv/bin/python3"
BRIDGE_CONSOLE = "/home/tech/starship-os/bridge_console.py"

# Icon drawing constants
ICON_SIZE = 22


def draw_status_icon(status_color):
    """Create a GdkPixbuf.Pixbuf with a colored dot."""
    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, ICON_SIZE, ICON_SIZE)
    cr = cairo.Context(surf)

    # transparent bg
    cr.set_operator(cairo.OPERATOR_CLEAR)
    cr.paint()
    cr.set_operator(cairo.OPERATOR_OVER)

    # outer glow ring
    r, g, b = status_color
    cx, cy = ICON_SIZE / 2, ICON_SIZE / 2
    radius = 7

    # glow
    cr.set_source_rgba(r, g, b, 0.2)
    cr.arc(cx, cy, radius + 3, 0, 2 * 3.14159)
    cr.fill()

    # dot
    cr.set_source_rgba(r, g, b, 0.95)
    cr.arc(cx, cy, radius, 0, 2 * 3.14159)
    cr.fill()

    # highlight
    cr.set_source_rgba(1, 1, 1, 0.2)
    cr.arc(cx - 2, cy - 2, 3, 0, 2 * 3.14159)
    cr.fill()

    return Gdk.pixbuf_get_from_surface(surf, 0, 0, ICON_SIZE, ICON_SIZE)


def load_status():
    try:
        with open(STATUS_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def overall_health(data):
    """Determine overall status color and text."""
    if not data:
        return (0.53, 0.20, 0.33), "offline"  # #FF3355

    agents = data.get("agents", {})
    if not agents:
        return (0.53, 0.20, 0.33), "no agents"

    statuses = {a.get("status", "offline") for a in agents.values()}

    if "offline" in statuses:
        return (1.0, 0.55, 0.0), "degraded"  # orange
    if "warning" in statuses:
        return (1.0, 0.55, 0.0), "degraded"
    if all(s == "online" for s in statuses):
        return (0.0, 0.8, 0.53), "online"  # green
    return (0.0, 0.83, 1.0), "active"  # cyan


class StarshipTray:
    def __init__(self):
        self.status_icon = Gtk.StatusIcon()
        self.status_icon.set_title("Starship OS")
        self.status_icon.set_tooltip_text("Starship OS — Starting...")
        self.status_icon.set_visible(True)
        self.status_icon.connect("activate", self.on_activate)
        self.status_icon.connect("popup-menu", self.on_popup_menu)

        self.menu = Gtk.Menu()

        # Header item (disabled)
        header = Gtk.MenuItem(label="Starship OS Bridge")
        header.set_sensitive(False)
        self.menu.append(header)
        self.menu.append(Gtk.SeparatorMenuItem())

        open_dash = Gtk.MenuItem(label="Open Dashboard")
        open_dash.connect("activate", lambda *_: self.open_url(DASHBOARD_URL))
        self.menu.append(open_dash)

        open_bridge = Gtk.MenuItem(label="Open Bridge Console")
        open_bridge.connect("activate", lambda *_: self.launch_bridge())
        self.menu.append(open_bridge)

        restart_dash = Gtk.MenuItem(label="Restart Dashboard Server")
        restart_dash.connect("activate", lambda *_: self.restart_dashboard())
        self.menu.append(restart_dash)

        self.menu.append(Gtk.SeparatorMenuItem())

        quit_item = Gtk.MenuItem(label="Quit")
        quit_item.connect("activate", lambda *_: Gtk.main_quit())
        self.menu.append(quit_item)

        self.menu.show_all()

        # Initial icon
        pix = draw_status_icon((0.53, 0.20, 0.33))
        self.status_icon.set_from_pixbuf(pix)

        # Poll timer
        GLib.timeout_add_seconds(POLL_INTERVAL, self.poll)

    def poll(self):
        data = load_status()
        color, label = overall_health(data)
        pix = draw_status_icon(color)
        self.status_icon.set_from_pixbuf(pix)

        if data and data.get("agents"):
            agents = data["agents"]
            agent_lines = [f"  {n}: {a.get('status', '?')}" for n, a in agents.items()]
            tooltip = "Starship OS — " + label.upper() + "\n" + "\n".join(agent_lines)
        else:
            tooltip = "Starship OS — " + label.upper()

        self.status_icon.set_tooltip_text(tooltip)
        return True

    def on_activate(self, icon):
        self.open_url(DASHBOARD_URL)

    def on_popup_menu(self, icon, button, time):
        self.menu.popup(None, None, Gtk.StatusIcon.position_menu, self.status_icon, button, time)

    def open_url(self, url):
        subprocess.Popen(["xdg-open", url])

    def launch_bridge(self):
        subprocess.Popen([HERMES_PYTHON, BRIDGE_CONSOLE])

    def restart_dashboard(self):
        try:
            subprocess.run(["pkill", "-f", DASHBOARD_SERVER], capture_output=True, timeout=5)
        except Exception:
            pass
        time.sleep(0.5)
        subprocess.Popen(
            [HERMES_PYTHON, DASHBOARD_SERVER],
            stdout=open("/tmp/dashboard-server.log", "w"),
            stderr=subprocess.STDOUT
        )


def main():
    signal.signal(signal.SIGINT, signal.SIG_DFL)

    # Ensure GTK runs in the main thread
    Gtk.init(sys.argv)
    tray = StarshipTray()
    Gtk.main()


if __name__ == "__main__":
    main()
