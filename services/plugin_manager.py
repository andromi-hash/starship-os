#!/usr/bin/env python3
"""
Starship OS Plugin Manager

Core plugin system that enables third parties to extend agents with new tools,
skills, webhook handlers, and NATS integrations.

Plugins live in /opt/agnetic/plugins/ (production) or ./plugins/ (dev mode).
Each plugin is a directory with:
    plugins/my-plugin/
        plugin.yaml          # metadata
        __init__.py          # Python entry point
        SKILL.md             # optional skill file
        tools/               # optional tool definitions
"""

import argparse
import hashlib
import importlib
import importlib.util
import json
import os
import secrets
import shutil
import subprocess
import sys
import textwrap
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import yaml


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PLUGINS_SYSTEM_DIR = Path("/opt/agnetic/plugins")
PLUGINS_DEV_DIR = Path("plugins")
CONFIG_PATH = Path("/etc/agnetic/plugins.yaml")
MARKETPLACE_URL = "https://marketplace.agnetic.ai/plugins"

AGNETIC_VERSION = "0.3.0"

SEVERITY_CRITICAL = "critical"
SEVERITY_HIGH = "high"
SEVERITY_MEDIUM = "medium"
SEVERITY_LOW = "low"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class ToolDefinition:
    name: str
    description: str
    parameters: dict[str, dict[str, Any]]
    module: Any = None
    function_name: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }


@dataclass
class WebhookHandler:
    event: str
    action: str
    callback: Callable | None = None
    module: Any = None


@dataclass
class NATSHandler:
    subject: str
    handler: str
    callback: Callable | None = None
    module: Any = None


@dataclass
class PluginDependency:
    python_packages: list[str] = field(default_factory=list)
    agnetic_services: list[str] = field(default_factory=list)


@dataclass
class PluginConfig:
    key: str
    config_type: str
    env_var: str = ""
    default: Any = None
    required: bool = False


@dataclass
class PluginManifest:
    name: str
    version: str
    description: str
    author: str
    license: str = "MIT"
    min_version: str = "0.1.0"
    homepage: str = ""
    tools: list[ToolDefinition] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    webhook_handlers: list[WebhookHandler] = field(default_factory=list)
    nats_handlers: list[NATSHandler] = field(default_factory=list)
    dependencies: PluginDependency = field(default_factory=PluginDependency)
    config: list[PluginConfig] = field(default_factory=list)


@dataclass
class PluginState:
    name: str
    enabled: bool
    loaded: bool
    version: str
    path: Path
    manifest: PluginManifest | None = None
    load_error: str = ""
    loaded_at: float = 0.0


@dataclass
class PluginSecurityReport:
    name: str
    verified: bool
    signature_valid: bool = False
    permissions: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    scanned_at: str = ""


@dataclass
class AgentPluginConfig:
    allow: list[str] = field(default_factory=list)
    deny: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# PluginManager
# ---------------------------------------------------------------------------

class PluginManager:
    """Core plugin management system for Starship OS."""

    def __init__(self, config_path: Path | str | None = None):
        self.config_path = Path(config_path) if config_path else CONFIG_PATH
        self.config: dict[str, Any] = {}
        self.plugins_dir: Path = PLUGINS_SYSTEM_DIR
        self.states: dict[str, PluginState] = {}
        self._loaded_modules: dict[str, Any] = {}
        self._registered_tools: dict[str, dict[str, ToolDefinition]] = {}
        self._registered_webhooks: dict[str, list[WebhookHandler]] = {}
        self._registered_nats: dict[str, NATSHandler] = {}
        self._agent_configs: dict[str, AgentPluginConfig] = {}
        self._load_config()

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def _load_config(self) -> None:
        """Load plugins configuration from /etc/agnetic/plugins.yaml."""
        if self.config_path.exists():
            try:
                with open(self.config_path) as f:
                    self.config = yaml.safe_load(f) or {}
            except Exception:
                self.config = {}
        else:
            self.config = {}

        plugins_section = self.config.get("plugins", {})
        configured_dir = plugins_section.get("dir", "")

        if configured_dir:
            self.plugins_dir = Path(configured_dir)
        else:
            if PLUGINS_DEV_DIR.exists():
                self.plugins_dir = PLUGINS_DEV_DIR
            else:
                self.plugins_dir = PLUGINS_SYSTEM_DIR

        per_agent = plugins_section.get("per_agent", {})
        for agent_name, agent_cfg in per_agent.items():
            self._agent_configs[agent_name] = AgentPluginConfig(
                allow=agent_cfg.get("allow", []),
                deny=agent_cfg.get("deny", []),
            )

    def _save_config(self) -> None:
        """Persist configuration to disk."""
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.config_path, "w") as f:
            yaml.dump(self.config, f, default_flow_style=False, sort_keys=False)

    def _get_auto_load(self) -> bool:
        return self.config.get("plugins", {}).get("auto_load", True)

    def _get_sandbox(self) -> bool:
        return self.config.get("plugins", {}).get("sandbox", True)

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def discover(self) -> list[str]:
        """Scan the plugins directory and register discovered plugins."""
        self.plugins_dir.mkdir(parents=True, exist_ok=True)
        discovered: list[str] = []

        for entry in sorted(self.plugins_dir.iterdir()):
            if not entry.is_dir():
                continue
            manifest_path = entry / "plugin.yaml"
            if not manifest_path.exists():
                continue
            try:
                manifest = self._parse_manifest(manifest_path)
            except Exception as exc:
                print(f"[plugin_manager] WARNING: Failed to parse {manifest_path}: {exc}")
                continue

            if manifest.name not in self.states:
                self.states[manifest.name] = PluginState(
                    name=manifest.name,
                    enabled=True,
                    loaded=False,
                    version=manifest.version,
                    path=entry,
                    manifest=manifest,
                )
            else:
                state = self.states[manifest.name]
                state.path = entry
                state.manifest = manifest
                state.version = manifest.version

            discovered.append(manifest.name)

        return discovered

    # ------------------------------------------------------------------
    # Manifest parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_manifest(path: Path) -> PluginManifest:
        """Parse a plugin.yaml manifest file."""
        with open(path) as f:
            raw = yaml.safe_load(f)

        provides = raw.get("provides", {})
        tools_raw = provides.get("tools", [])
        skills_raw = provides.get("skills", [])
        webhooks_raw = provides.get("webhook_handlers", [])
        nats_raw = provides.get("nats_handlers", [])

        deps_raw = raw.get("dependencies", {})
        deps = PluginDependency(
            python_packages=deps_raw.get("python", []),
            agnetic_services=deps_raw.get("services", []),
        )

        config_raw = raw.get("config", {})
        configs: list[PluginConfig] = []
        for key, cdef in config_raw.items():
            configs.append(PluginConfig(
                key=key,
                config_type=cdef.get("type", "string"),
                env_var=cdef.get("env", ""),
                default=cdef.get("default"),
                required=cdef.get("required", False),
            ))

        tools: list[ToolDefinition] = []
        for t in tools_raw:
            tools.append(ToolDefinition(
                name=t["name"],
                description=t.get("description", ""),
                parameters=t.get("parameters", {}),
            ))

        webhooks: list[WebhookHandler] = []
        for wh in webhooks_raw:
            webhooks.append(WebhookHandler(
                event=wh.get("event", ""),
                action=wh.get("action", ""),
            ))

        nats_handlers: list[NATSHandler] = []
        for nh in nats_raw:
            nats_handlers.append(NATSHandler(
                subject=nh.get("subject", ""),
                handler=nh.get("handler", ""),
            ))

        return PluginManifest(
            name=raw["name"],
            version=raw.get("version", "0.0.1"),
            description=raw.get("description", ""),
            author=raw.get("author", "Unknown"),
            license=raw.get("license", "MIT"),
            min_version=raw.get("min_version", "0.1.0"),
            homepage=raw.get("homepage", ""),
            tools=tools,
            skills=skills_raw,
            webhook_handlers=webhooks,
            nats_handlers=nats_handlers,
            dependencies=deps,
            config=configs,
        )

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    def load(self, name: str) -> bool:
        """Load a specific plugin by name."""
        if name not in self.states:
            discovered = self.discover()
            if name not in self.states:
                print(f"[plugin_manager] Plugin '{name}' not found.")
                return False

        state = self.states[name]

        if state.loaded:
            return True

        if not state.enabled:
            print(f"[plugin_manager] Plugin '{name}' is disabled. Enable it first.")
            return False

        if state.manifest and not self._check_version_compat(state.manifest.min_version):
            state.load_error = f"Requires Agnetic >= {state.manifest.min_version}, running {AGNETIC_VERSION}"
            print(f"[plugin_manager] {state.load_error}")
            return False

        if state.manifest and state.manifest.dependencies.python_packages:
            missing = self._check_python_deps(state.manifest.dependencies.python_packages)
            if missing:
                state.load_error = f"Missing Python packages: {', '.join(missing)}"
                print(f"[plugin_manager] {state.load_error}")
                return False

        try:
            self._load_plugin_module(name, state)
            state.loaded = True
            state.loaded_at = time.time()
            state.load_error = ""
            self._register_plugin_tools(name, state)
            self._register_plugin_webhooks(name, state)
            self._register_plugin_nats(name, state)
            print(f"[plugin_manager] Loaded plugin '{name}' v{state.version}")
            return True
        except Exception as exc:
            state.load_error = str(exc)
            print(f"[plugin_manager] Failed to load '{name}': {exc}")
            return False

    def load_all(self) -> dict[str, bool]:
        """Load all discovered and enabled plugins."""
        self.discover()
        results: dict[str, bool] = {}
        for name, state in self.states.items():
            if state.enabled:
                results[name] = self.load(name)
        return results

    def _load_plugin_module(self, name: str, state: PluginState) -> Any:
        """Import the plugin's __init__.py entry point."""
        init_path = state.path / "__init__.py"
        if not init_path.exists():
            module_name = f"agnetic_plugin_{name.replace('-', '_')}"
            spec = importlib.util.spec_from_file_location(
                module_name,
                str(state.path / "__init__.py"),
            )
            if spec is None or spec.loader is None:
                raise ImportError(f"No __init__.py found for plugin '{name}'")

        module_name = f"agnetic_plugin_{name.replace('-', '_')}"
        init_path = state.path / "__init__.py"

        if not init_path.exists():
            state._plugin_module = None
            return None

        spec = importlib.util.spec_from_file_location(module_name, str(init_path))
        if spec is None or spec.loader is None:
            raise ImportError(f"Cannot load module spec for plugin '{name}'")

        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        self._loaded_modules[name] = module
        return module

    def _register_plugin_tools(self, name: str, state: PluginState) -> None:
        """Register a plugin's tools into the global tool registry."""
        if not state.manifest:
            return

        module = self._loaded_modules.get(name)

        self._registered_tools.setdefault(name, {})
        for tool_def in state.manifest.tools:
            func = None
            if module:
                func = getattr(module, tool_def.name, None)
                if func is None and hasattr(module, "tools"):
                    tool_map = getattr(module, "tools", {})
                    if isinstance(tool_map, dict):
                        func = tool_map.get(tool_def.name)
            tool_def.module = module
            tool_def.function_name = tool_def.name
            if func:
                tool_def.module = module
            self._registered_tools[name][tool_def.name] = tool_def

    def _register_plugin_webhooks(self, name: str, state: PluginState) -> None:
        """Register a plugin's webhook handlers."""
        if not state.manifest:
            return

        module = self._loaded_modules.get(name)
        for wh in state.manifest.webhook_handlers:
            if module:
                handler_func = getattr(module, wh.action, None)
                if handler_func:
                    wh.callback = handler_func
            wh.module = module
            self._registered_webhooks.setdefault(wh.event, [])
            self._registered_webhooks[wh.event].append(wh)

    def _register_plugin_nats(self, name: str, state: PluginState) -> None:
        """Register a plugin's NATS handlers."""
        if not state.manifest:
            return

        module = self._loaded_modules.get(name)
        for nh in state.manifest.nats_handlers:
            if module:
                handler_func = getattr(module, nh.handler, None)
                if handler_func:
                    nh.callback = handler_func
            nh.module = module
            self._registered_nats[nh.subject] = nh

    # ------------------------------------------------------------------
    # Plugin access
    # ------------------------------------------------------------------

    def get_tools(self, agent_name: str) -> list[ToolDefinition]:
        """Return tools available to the given agent."""
        agent_cfg = self._agent_configs.get(agent_name)
        result: list[ToolDefinition] = []

        for plugin_name, tools in self._registered_tools.items():
            if not self._is_plugin_enabled_for_agent(plugin_name, agent_name):
                continue
            for tool_def in tools.values():
                result.append(tool_def)

        return result

    def get_skills(self, agent_name: str) -> list[str]:
        """Return skill file paths available to the given agent."""
        skills: list[str] = []

        for name, state in self.states.items():
            if not state.loaded or not state.manifest:
                continue
            if not self._is_plugin_enabled_for_agent(name, agent_name):
                continue
            skill_file = state.path / "SKILL.md"
            if skill_file.exists():
                skills.append(str(skill_file))

        return skills

    def get_webhook_handlers(self, event: str) -> list[WebhookHandler]:
        """Return webhook handlers for a given event type."""
        return self._registered_webhooks.get(event, [])

    def get_nats_handler(self, subject: str) -> NATSHandler | None:
        """Return the NATS handler matching a subject (supports wildcard suffix)."""
        exact = self._registered_nats.get(subject)
        if exact:
            return exact
        for pattern, handler in self._registered_nats.items():
            if pattern.endswith(".>"):
                prefix = pattern[:-2]
                if subject.startswith(prefix):
                    return handler
        return None

    def _is_plugin_enabled_for_agent(self, plugin_name: str, agent_name: str) -> bool:
        """Check if a plugin is allowed for a specific agent."""
        state = self.states.get(plugin_name)
        if not state or not state.enabled or not state.loaded:
            return False

        agent_cfg = self._agent_configs.get(agent_name)
        if agent_cfg is None:
            return True

        if agent_cfg.deny:
            if "all" in agent_cfg.deny or plugin_name in agent_cfg.deny:
                return False

        if agent_cfg.allow:
            if "all" in agent_cfg.allow or plugin_name in agent_cfg.allow:
                return True
            return False

        return True

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def enable(self, name: str) -> bool:
        """Enable a plugin."""
        if name not in self.states:
            print(f"[plugin_manager] Plugin '{name}' not found.")
            return False
        self.states[name].enabled = True
        self._persist_enabled_state()
        print(f"[plugin_manager] Plugin '{name}' enabled.")
        return True

    def disable(self, name: str) -> bool:
        """Disable a plugin. Unloads it if currently loaded."""
        if name not in self.states:
            print(f"[plugin_manager] Plugin '{name}' not found.")
            return False
        state = self.states[name]
        if state.loaded:
            self._unload(name)
        state.enabled = False
        self._persist_enabled_state()
        print(f"[plugin_manager] Plugin '{name}' disabled.")
        return True

    def _unload(self, name: str) -> None:
        """Unload a plugin's module and remove its registrations."""
        self._registered_tools.pop(name, None)
        module = self._loaded_modules.pop(name, None)
        if module:
            module_name = module.__name__
            sys.modules.pop(module_name, None)
        state = self.states.get(name)
        if state:
            state.loaded = False
            state.loaded_at = 0.0

    def uninstall(self, name: str) -> bool:
        """Uninstall a plugin: disable, remove files, cleanup."""
        if name not in self.states:
            print(f"[plugin_manager] Plugin '{name}' not found.")
            return False

        state = self.states[name]
        if state.loaded:
            self._unload(name)

        plugin_dir = self.plugins_dir / name
        if plugin_dir.exists():
            shutil.rmtree(plugin_dir)

        del self.states[name]
        self._persist_enabled_state()
        print(f"[plugin_manager] Plugin '{name}' uninstalled.")
        return True

    def install_from_path(self, source: Path, name: str | None = None) -> bool:
        """Install a plugin from a local directory."""
        if not source.is_dir():
            print(f"[plugin_manager] Source '{source}' is not a directory.")
            return False

        manifest_path = source / "plugin.yaml"
        if not manifest_path.exists():
            print(f"[plugin_manager] No plugin.yaml found in '{source}'.")
            return False

        manifest = self._parse_manifest(manifest_path)
        plugin_name = name or manifest.name
        dest = self.plugins_dir / plugin_name

        if dest.exists():
            print(f"[plugin_manager] Plugin '{plugin_name}' already installed. Use update instead.")
            return False

        shutil.copytree(source, dest)

        if manifest.dependencies.python_packages:
            self._install_python_deps(manifest.dependencies.python_packages)

        self._run_setup_hook(dest)
        self.discover()
        print(f"[plugin_manager] Installed plugin '{plugin_name}' v{manifest.version}")
        return True

    def update(self, name: str) -> bool:
        """Update a plugin (placeholder for marketplace update)."""
        if name not in self.states:
            print(f"[plugin_manager] Plugin '{name}' not found.")
            return False

        state = self.states[name]
        print(f"[plugin_manager] Checking updates for '{name}' v{state.version}...")
        print(f"[plugin_manager] Marketplace update not yet implemented.")
        return False

    def verify(self, name: str) -> PluginSecurityReport:
        """Verify plugin integrity and generate a security report."""
        if name not in self.states:
            return PluginSecurityReport(
                name=name,
                verified=False,
                errors=[f"Plugin '{name}' not found"],
                scanned_at=datetime.utcnow().isoformat(),
            )

        state = self.states[name]
        report = PluginSecurityReport(
            name=name,
            verified=True,
            scanned_at=datetime.utcnow().isoformat(),
        )

        if not (state.path / "plugin.yaml").exists():
            report.verified = False
            report.errors.append("Missing plugin.yaml")

        if not (state.path / "__init__.py").exists():
            report.warnings.append("No __init__.py entry point")

        if state.manifest:
            report.permissions = self._derive_permissions(state.manifest)
            for pc in state.manifest.config:
                if pc.config_type == "env" and pc.required and not os.environ.get(pc.env_var):
                    report.warnings.append(f"Required env var {pc.env_var} not set")

            if state.manifest.min_version > AGNETIC_VERSION:
                report.verified = False
                report.errors.append(
                    f"Requires Agnetic >= {state.manifest.min_version}, running {AGNETIC_VERSION}"
                )

        checksum = self._compute_plugin_checksum(state.path)
        checksum_file = state.path / ".checksum"
        if checksum_file.exists():
            stored = checksum_file.read_text().strip()
            if stored != checksum:
                report.verified = False
                report.errors.append("Checksum mismatch — plugin files may have been modified")
            else:
                report.signature_valid = True
        else:
            checksum_file.write_text(checksum)
            report.signature_valid = True

        return report

    def _compute_plugin_checksum(self, plugin_dir: Path) -> str:
        """Compute SHA-256 checksum of all plugin files (excluding .checksum)."""
        h = hashlib.sha256()
        for f in sorted(plugin_dir.rglob("*")):
            if f.is_file() and f.name != ".checksum":
                h.update(f.read_bytes())
        return h.hexdigest()

    def _derive_permissions(self, manifest: PluginManifest) -> list[str]:
        """Derive permission list from manifest."""
        perms: list[str] = []
        if manifest.tools:
            perms.append("tools")
        if manifest.skills:
            perms.append("skills")
        if manifest.webhook_handlers:
            perms.append("webhooks")
        if manifest.nats_handlers:
            perms.append("nats")
        if manifest.dependencies.agnetentic_services:
            perms.append(f"services:{','.join(manifest.dependencies.agnetentic_services)}")
        return perms

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _check_version_compat(required: str) -> bool:
        """Simple semver comparison for min_version."""
        def parse(v: str) -> tuple[int, int, int]:
            parts = v.strip().split(".")
            nums = []
            for p in parts[:3]:
                try:
                    nums.append(int(p))
                except ValueError:
                    nums.append(0)
            while len(nums) < 3:
                nums.append(0)
            return (nums[0], nums[1], nums[2])

        return parse(AGNETIC_VERSION) >= parse(required)

    @staticmethod
    def _check_python_deps(packages: list[str]) -> list[str]:
        """Check which Python packages are missing."""
        missing: list[str] = []
        for pkg in packages:
            pkg_name = pkg.split(">=")[0].split("==")[0].split("<")[0].strip()
            pkg_import = pkg_name.replace("-", "_").lower()
            try:
                importlib.import_module(pkg_import)
            except ImportError:
                missing.append(pkg)
        return missing

    @staticmethod
    def _install_python_deps(packages: list[str]) -> None:
        """Install missing Python packages via pip."""
        for pkg in packages:
            pkg_name = pkg.split(">=")[0].split("==")[0].split("<")[0].strip()
            pkg_import = pkg_name.replace("-", "_").lower()
            try:
                importlib.import_module(pkg_import)
            except ImportError:
                print(f"[plugin_manager] Installing {pkg}...")
                subprocess.check_call(
                    [sys.executable, "-m", "pip", "install", pkg],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )

    @staticmethod
    def _run_setup_hook(plugin_dir: Path) -> None:
        """Run a setup.py or setup hook if present."""
        setup_script = plugin_dir / "setup.sh"
        if setup_script.exists():
            subprocess.run(["bash", str(setup_script)], cwd=str(plugin_dir))

        setup_py = plugin_dir / "setup.py"
        if setup_py.exists():
            subprocess.run(
                [sys.executable, str(setup_py), "install"],
                cwd=str(plugin_dir),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

    def _persist_enabled_state(self) -> None:
        """Save enabled/disabled state to a state file alongside the config."""
        state_file = self.config_path.parent / "plugins_state.json"
        state_data: dict[str, bool] = {}
        for name, state in self.states.items():
            state_data[name] = state.enabled
        state_file.parent.mkdir(parents=True, exist_ok=True)
        with open(state_file, "w") as f:
            json.dump(state_data, f, indent=2)

    def _load_enabled_state(self) -> None:
        """Load enabled/disabled state from disk."""
        state_file = self.config_path.parent / "plugins_state.json"
        if state_file.exists():
            with open(state_file) as f:
                state_data = json.load(f)
            for name, enabled in state_data.items():
                if name in self.states:
                    self.states[name].enabled = enabled

    # ------------------------------------------------------------------
    # Plugin Creator (interactive)
    # ------------------------------------------------------------------

    def create_plugin_interactive(self) -> Path | None:
        """Interactive wizard to scaffold a new plugin."""
        print("\n=== Starship OS Plugin Creator ===\n")

        name = input("Plugin name: ").strip()
        if not name:
            print("Aborted.")
            return None

        version = input("Version [0.1.0]: ").strip() or "0.1.0"
        description = input("Description: ").strip()
        author = input("Author: ").strip() or "Unknown"
        license_type = input("License [MIT]: ").strip() or "MIT"

        print("\nWhat does this plugin provide?")
        has_tools = input("  Tools? [Y/n]: ").strip().lower() != "n"
        has_skills = input("  Skills? [y/N]: ").strip().lower() == "y"
        has_webhooks = input("  Webhook handlers? [y/N]: ").strip().lower() == "y"
        has_nats = input("  NATS handlers? [y/N]: ").strip().lower() == "y"

        tool_names: list[str] = []
        if has_tools:
            raw = input("  Tool names (comma-separated): ").strip()
            tool_names = [t.strip() for t in raw.split(",") if t.strip()]

        skill_names: list[str] = []
        if has_skills:
            raw = input("  Skill names (comma-separated): ").strip()
            skill_names = [s.strip() for s in raw.split(",") if s.strip()]

        webhook_events: list[str] = []
        if has_webhooks:
            raw = input("  Webhook events (comma-separated, e.g. pull_request,push): ").strip()
            webhook_events = [e.strip() for e in raw.split(",") if e.strip()]

        nats_subjects: list[str] = []
        if has_nats:
            raw = input("  NATS subjects (comma-separated): ").strip()
            nats_subjects = [s.strip() for s in raw.split(",") if s.strip()]

        plugin_dir = self.plugins_dir / name
        plugin_dir.mkdir(parents=True, exist_ok=True)

        (plugin_dir / "tools").mkdir(exist_ok=True)

        # --- plugin.yaml ---
        provides_section: dict[str, Any] = {}
        if tool_names:
            tools_list = []
            for tn in tool_names:
                tools_list.append({
                    "name": tn,
                    "description": f"{tn} operation",
                    "parameters": {},
                })
            provides_section["tools"] = tools_list
        if skill_names:
            provides_section["skills"] = skill_names
        if webhook_events:
            wh_list = []
            for ev in webhook_events:
                wh_list.append({"event": ev, "action": f"handle_{ev}"})
            provides_section["webhook_handlers"] = wh_list
        if nats_subjects:
            nats_list = []
            for subj in nats_subjects:
                nats_list.append({"subject": subj, "handler": f"handle_{subj.replace('.', '_')}"})
            provides_section["nats_handlers"] = nats_list

        manifest_data: dict[str, Any] = {
            "name": name,
            "version": version,
            "description": description,
            "author": author,
            "license": license_type,
            "min_version": "0.1.0",
        }
        if provides_section:
            manifest_data["provides"] = provides_section

        manifest_path = plugin_dir / "plugin.yaml"
        with open(manifest_path, "w") as f:
            yaml.dump(manifest_data, f, default_flow_style=False, sort_keys=False)

        # --- __init__.py ---
        init_lines = [
            f'"""',
            f"{description}",
            f'"""',
            "",
        ]

        if tool_names:
            init_lines.append("# Tool implementations")
            for tn in tool_names:
                init_lines.append(f"")
                init_lines.append(f"def {tn}(**kwargs):")
                init_lines.append(f'    """Execute {tn}."""')
                init_lines.append(f"    raise NotImplementedError")
            init_lines.append("")

        init_lines.append("# Tool registry for tool_loader style access")
        init_lines.append("tools = {")
        for tn in tool_names:
            init_lines.append(f'    "{tn}": {tn},')
        init_lines.append("}")
        init_lines.append("")

        if webhook_events:
            init_lines.append("# Webhook handlers")
            for ev in webhook_events:
                init_lines.append(f"")
                init_lines.append(f"def handle_{ev}(event_data):")
                init_lines.append(f'    """Handle {ev} webhook events."""')
                init_lines.append(f"    raise NotImplementedError")
            init_lines.append("")

        if nats_subjects:
            init_lines.append("# NATS handlers")
            for subj in nats_subjects:
                safe_name = subj.replace(".", "_").replace(">", "wildcard")
                init_lines.append(f"")
                init_lines.append(f"def handle_{safe_name}(msg):")
                init_lines.append(f'    """Handle NATS messages on {subj}."""')
                init_lines.append(f"    raise NotImplementedError")
            init_lines.append("")

        init_path = plugin_dir / "__init__.py"
        init_path.write_text("\n".join(init_lines) + "\n")

        # --- SKILL.md ---
        if has_skills:
            skill_lines = [
                f"# {name} Skills",
                "",
                f"Plugin: {name} v{version}",
                "",
            ]
            for sn in skill_names:
                skill_lines.append(f"## {sn}")
                skill_lines.append("")
                skill_lines.append(f"Skill provided by the {name} plugin.")
                skill_lines.append("")
            skill_path = plugin_dir / "SKILL.md"
            skill_path.write_text("\n".join(skill_lines) + "\n")

        # --- tool stubs ---
        for tn in tool_names:
            tool_file = plugin_dir / "tools" / f"{tn}.py"
            tool_file.write_text(
                f'def {tn}(**kwargs):\n'
                f'    """Implement {tn} tool logic here."""\n'
                f'    raise NotImplementedError\n'
            )

        print(f"\nPlugin scaffolded at: {plugin_dir}")
        print(f"  {manifest_path}")
        print(f"  {plugin_dir / '__init__.py'}")
        if has_skills:
            print(f"  {plugin_dir / 'SKILL.md'}")
        print(f"\nEdit the files to implement your plugin, then install it with:")
        print(f"  python3 plugin_manager.py install {plugin_dir}")

        return plugin_dir


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def cmd_list(manager: PluginManager) -> None:
    manager.discover()
    if not manager.states:
        print("No plugins found.")
        return

    print(f"{'Name':<30} {'Version':<10} {'Enabled':<10} {'Loaded':<10}")
    print("-" * 60)
    for name, state in sorted(manager.states.items()):
        enabled = "yes" if state.enabled else "no"
        loaded = "yes" if state.loaded else "no"
        print(f"{name:<30} {state.version:<10} {enabled:<10} {loaded:<10}")


def cmd_install(manager: PluginManager, args: list[str]) -> None:
    if not args:
        print("Usage: plugin_manager.py install <path-or-name>")
        return
    source = Path(args[0])
    name = args[1] if len(args) > 1 else None
    manager.install_from_path(source, name)


def cmd_enable(manager: PluginManager, args: list[str]) -> None:
    if not args:
        print("Usage: plugin_manager.py enable <name>")
        return
    manager.enable(args[0])


def cmd_disable(manager: PluginManager, args: list[str]) -> None:
    if not args:
        print("Usage: plugin_manager.py disable <name>")
        return
    manager.disable(args[0])


def cmd_remove(manager: PluginManager, args: list[str]) -> None:
    if not args:
        print("Usage: plugin_manager.py remove <name>")
        return
    manager.uninstall(args[0])


def cmd_info(manager: PluginManager, args: list[str]) -> None:
    if not args:
        print("Usage: plugin_manager.py info <name>")
        return
    name = args[0]
    manager.discover()
    state = manager.states.get(name)
    if not state:
        print(f"Plugin '{name}' not found.")
        return

    manifest = state.manifest
    print(f"Name:        {name}")
    print(f"Version:     {state.version}")
    print(f"Enabled:     {state.enabled}")
    print(f"Loaded:      {state.loaded}")
    print(f"Path:        {state.path}")

    if manifest:
        print(f"Description: {manifest.description}")
        print(f"Author:      {manifest.author}")
        print(f"License:     {manifest.license}")
        print(f"Min Version: {manifest.min_version}")

        if manifest.tools:
            print(f"\nTools ({len(manifest.tools)}):")
            for t in manifest.tools:
                print(f"  - {t.name}: {t.description}")

        if manifest.skills:
            print(f"\nSkills: {', '.join(manifest.skills)}")

        if manifest.webhook_handlers:
            print(f"\nWebhook Handlers:")
            for wh in manifest.webhook_handlers:
                print(f"  - event={wh.event}, action={wh.action}")

        if manifest.nats_handlers:
            print(f"\nNATS Handlers:")
            for nh in manifest.nats_handlers:
                print(f"  - subject={nh.subject}, handler={nh.handler}")

        if manifest.config:
            print(f"\nConfiguration:")
            for pc in manifest.config:
                req = "required" if pc.required else f"default={pc.default}"
                print(f"  - {pc.key} ({pc.config_type}): {req}")

        if manifest.dependencies.python_packages:
            print(f"\nPython Dependencies: {', '.join(manifest.dependencies.python_packages)}")
        if manifest.dependencies.agnetentic_services:
            print(f"Service Dependencies: {', '.join(manifest.dependencies.agnetentic_services)}")

    if state.load_error:
        print(f"\nLoad Error: {state.load_error}")


def cmd_verify(manager: PluginManager, args: list[str]) -> None:
    if not args:
        print("Usage: plugin_manager.py verify <name>")
        return
    name = args[0]
    manager.discover()
    report = manager.verify(name)

    print(f"\nSecurity Report: {report.name}")
    print(f"  Verified:       {report.verified}")
    print(f"  Signature Valid: {report.signature_valid}")
    print(f"  Scanned At:     {report.scanned_at}")

    if report.permissions:
        print(f"  Permissions:     {', '.join(report.permissions)}")
    if report.warnings:
        print(f"\n  Warnings:")
        for w in report.warnings:
            print(f"    - {w}")
    if report.errors:
        print(f"\n  Errors:")
        for e in report.errors:
            print(f"    - {e}")

    if not report.errors and not report.warnings:
        print(f"\n  All checks passed.")


def cmd_create(manager: PluginManager) -> None:
    manager.create_plugin_interactive()


def cmd_load(manager: PluginManager, args: list[str]) -> None:
    """Load a plugin or all plugins."""
    manager.discover()
    if not args:
        results = manager.load_all()
        for name, ok in results.items():
            status = "loaded" if ok else "FAILED"
            print(f"  {name}: {status}")
    else:
        ok = manager.load(args[0])
        if ok:
            print(f"Plugin '{args[0]}' loaded.")
        else:
            print(f"Failed to load '{args[0]}'.")


def cmd_help() -> None:
    print("""\
Starship OS Plugin Manager

Usage:
  python3 plugin_manager.py <command> [args]

Commands:
  list                              List installed plugins
  install <path> [name]             Install plugin from local directory
  enable <name>                     Enable a plugin
  disable <name>                    Disable a plugin
  remove <name>                     Uninstall a plugin
  info <name>                       Show plugin details
  verify <name>                     Verify plugin integrity
  load [name]                       Load a plugin (or all if no name given)
  create                            Interactive plugin creator wizard
  help                              Show this help message

Examples:
  python3 plugin_manager.py list
  python3 plugin_manager.py install ./my-plugin
  python3 plugin_manager.py load github-integration
  python3 plugin_manager.py verify github-integration
""")


def main() -> None:
    manager = PluginManager()

    if len(sys.argv) < 2:
        cmd_help()
        sys.exit(0)

    command = sys.argv[1]
    extra_args = sys.argv[2:]

    commands = {
        "list": lambda: cmd_list(manager),
        "install": lambda: cmd_install(manager, extra_args),
        "enable": lambda: cmd_enable(manager, extra_args),
        "disable": lambda: cmd_disable(manager, extra_args),
        "remove": lambda: cmd_remove(manager, extra_args),
        "info": lambda: cmd_info(manager, extra_args),
        "verify": lambda: cmd_verify(manager, extra_args),
        "load": lambda: cmd_load(manager, extra_args),
        "create": lambda: cmd_create(manager),
        "help": lambda: cmd_help(),
    }

    handler = commands.get(command)
    if handler:
        handler()
    else:
        print(f"Unknown command: {command}")
        cmd_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
