Starship OS — StarAgent Windows Agent
========================================

This package installs the Starship OS telemetry agent on Windows.
The agent collects system metrics and publishes them to your
Starship OS hub via NATS, appearing in the Shield dashboard tab.

REQUIREMENTS
------------
- Windows 10/11 or Windows Server 2019+
- Administrator access for installation
- Network connectivity to your Starship OS NATS hub

INSTALLATION
------------
1. Extract this ZIP to a folder
2. Right-click install.bat → "Run as administrator"
3. Enter your hub's NATS URL and token when prompted
4. The agent installs as a Windows service and starts automatically

CONFIGURATION
-------------
After installation, you can change settings by running:
  configure.bat  (run as Administrator)

Or manually edit: C:\Program Files\Starship\Agent\staragent.yaml

FINDING YOUR NATS TOKEN
------------------------
On your Starship OS hub, the NATS shared token is in:
  /etc/starship/nats/fleet-bus.conf

Look for: token: "__STARSHIP_NATS_TOKEN__"
(replace __STARSHIP_NATS_TOKEN__ with the actual token)

The NATS URL is typically: nats://<hub-ip>:4222

VERIFICATION
------------
After starting the service:
  sc query StarshipStarAgent   → check service status
  type "C:\ProgramData\Starship\logs\staragent.log"  → view logs

On the hub dashboard, navigate to the Shield tab (⛨).
After ~10 seconds, the agent should appear in the endpoints list.

UNINSTALL
---------
1. Right-click uninstall.bat → "Run as administrator"
2. The service, files, and environment variables are removed.

TROUBLESHOOTING
---------------
- Service fails to start: Check the log file and verify the NATS
  URL and token are correct.

- "Connection refused": The hub's NATS port (4222) may be firewalled.
  Ensure the agent can reach <hub-ip>:4222.

- No data in Shield: Verify the agent's hostname is unique.
  Run: configure.bat and set a custom hostname.

FILES
-----
staragent.exe        — The agent binary
staragent.yaml       — Configuration file
install.bat          — Install as Windows service
uninstall.bat        — Remove service and files
configure.bat        — Change NATS connection settings
README.txt           — This file
