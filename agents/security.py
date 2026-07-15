#!/usr/bin/env python3
"""
Starship OS Security — NATS auth, encrypted config, secrets management.

Provides:
- Per-agent NATS tokens with subject-level permissions
- AES-256-GCM encrypted config files
- Secure secret storage
"""

import os
import json
import hashlib
import secrets
import base64
from pathlib import Path
from datetime import datetime

SECRETS_DIR = Path(os.getenv("AGNETIC_SECRETS", "/etc/agnetic/secrets"))
CONFIG_DIR = Path(os.getenv("AGNETIC_CONFIG", "/etc/agnetic"))


# ─── NATS Authentication ────────────────────────────────────────────
def generate_nats_token():
    """Generate a cryptographically secure NATS token."""
    return secrets.token_hex(32)


def generate_agent_tokens():
    """Generate tokens for all agents and save to secrets."""
    SECRETS_DIR.mkdir(parents=True, exist_ok=True)

    agents = ["proxy", "romi", "ergo", "staragent", "dashboard"]
    tokens = {}

    for agent in agents:
        token = generate_nats_token()
        tokens[agent] = token
        token_file = SECRETS_DIR / f"{agent}.token"
        token_file.write_text(token)
        token_file.chmod(0o600)

    # Generate operator token
    operator_token = generate_nats_token()
    tokens["operator"] = operator_token
    (SECRETS_DIR / "operator.token").write_text(operator_token)
    (SECRETS_DIR / "operator.token").chmod(0o600)

    return tokens


def load_agent_token(agent_name):
    """Load NATS token for an agent."""
    token_file = SECRETS_DIR / f"{agent_name}.token"
    if token_file.exists():
        return token_file.read_text().strip()

    # Fallback to environment
    return os.getenv(f"AGENTIC_{agent_name.upper()}_TOKEN", "")


def generate_nats_config(tokens=None):
    """Generate a NATS server config with per-agent auth and subject permissions."""
    if not tokens:
        tokens = generate_nats_token()
        tokens = {"operator": tokens}

    accounts = []

    # System account (operator)
    accounts.append(f"""  system:
    users: [
      {{user: "operator", password: "{tokens.get('operator', generate_nats_token())}"}}
    ]
    imports: []
    exports: []
""")

    # Agent accounts with subject-level permissions
    agent_permissions = {
        "proxy": {
            "publish": ["agnetic.agent.proxy.>", "agnetic.telemetry.>"],
            "subscribe": ["agnetic.agent.proxy.command.>", "agnetic.telemetry.>", "system.>"],
        },
        "romi": {
            "publish": ["agnetic.agent.romi.>", "romatic.telemetry.>"],
            "subscribe": ["agnetic.agent.romi.command.>", "agnetic.telemetry.>", "system.>"],
        },
        "ergo": {
            "publish": ["agnetic.agent.ergo.>", "agnetic.workflow.>", "agnetic.telemetry.>"],
            "subscribe": ["agnetic.agent.ergo.command.>", "agnetic.workflow.>", "agnetic.telemetry.>", "system.>"],
        },
        "staragent": {
            "publish": ["agnetic.telemetry.>"],
            "subscribe": ["system.>"],
        },
        "dashboard": {
            "publish": ["agnetic.agent.proxy.command.>", "agnetic.agent.romi.command.>", "agnetic.agent.ergo.command.>", "agnetic.workflow.>"],
            "subscribe": ["agnetic.agent.>.status", "agnetic.agent.>.event.>", "agnetic.telemetry.>"],
        },
    }

    for agent, perms in agent_permissions.items():
        token = tokens.get(agent, generate_nats_token())
        pub_subjects = json.dumps(perms["publish"])
        sub_subjects = json.dumps(perms["subscribe"])
        accounts.append(f"""  {agent}:
    users: [
      {{user: "{agent}", password: "{token}"}}
    ]
    exports: [
      {{service: "agnetic.agent.{agent}.status"}},
      {{service: "agnetic.agent.{agent}.event.>"}}
    ]
    imports: []
    authorization: {{
      account: {agent}
      publish: {{
        allow: {pub_subjects}
      }}
      subscribe: {{
        allow: {sub_subjects}
      }}
    }}
""")

    config = f"""# Starship OS — NATS Server Configuration (auto-generated)
# Generated: {datetime.now().isoformat()}
# DO NOT EDIT MANUALLY — use generate_nats_config() or security.generate_nats_config()

port: 4222
http_port: 8222

max_payload: 1048576
max_pending: 67108864

jetstream {{
  store_dir: /var/lib/agnetic/nats
  max_mem: 268435456
  max_file: 10737418240
}}

# Authentication
authorization {{
  account: system
}}

accounts: {{
{"".join(accounts)}
}}

# Logging
log_file: /var/log/agnetic/nats.log
debug: false
trace: false
"""
    return config


# ─── Encrypted Config ───────────────────────────────────────────────
def _derive_key(password, salt=None):
    """Derive an AES key from a password using PBKDF2."""
    if salt is None:
        salt = os.urandom(16)
    key = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, 100000)
    return key, salt


def encrypt_config(data, password):
    """Encrypt config data with AES-256-GCM (using only stdlib + hashlib).

    Returns base64-encoded salt:ciphertext:tag.
    """
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    key, salt = _derive_key(password)
    nonce = os.urandom(12)

    plaintext = json.dumps(data).encode()
    aesgcm = AESGCM(key)
    ciphertext = aesgcm.encrypt(nonce, plaintext, None)

    # salt:nonce:ciphertext
    encrypted = salt + nonce + ciphertext
    return base64.b64encode(encrypted).decode()


def decrypt_config(encrypted_b64, password):
    """Decrypt config data encrypted with encrypt_config()."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    encrypted = base64.b64decode(encrypted_b64)
    salt = encrypted[:16]
    nonce = encrypted[16:28]
    ciphertext = encrypted[28:]

    key, _ = _derive_key(password, salt)
    aesgcm = AESGCM(key)
    plaintext = aesgcm.decrypt(nonce, ciphertext, None)
    return json.loads(plaintext)


def save_encrypted_config(data, path, password):
    """Save config as encrypted JSON file."""
    encrypted = encrypt_config(data, password)
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"encrypted": True, "data": encrypted}))
    p.chmod(0o600)


def load_encrypted_config(path, password):
    """Load encrypted config from file."""
    raw = json.loads(Path(path).read_text())
    if raw.get("encrypted"):
        return decrypt_config(raw["data"], password)
    return raw


# ─── Secrets Manager ────────────────────────────────────────────────
class SecretsManager:
    """Encrypted secrets storage for API keys, tokens, etc."""

    def __init__(self, password=None, secrets_dir=None):
        self.password = password or os.getenv("AGENTIC_MASTER_PASSWORD", "")
        self.secrets_dir = Path(secrets_dir or SECRETS_DIR)
        self.secrets_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, name):
        return self.secrets_dir / f"{name}.enc"

    def set(self, name, value):
        """Store an encrypted secret."""
        if not self.password:
            raise ValueError("Master password required")
        encrypted = encrypt_config({"value": value}, self.password)
        self._path(name).write_text(encrypted)
        self._path(name).chmod(0o600)

    def get(self, name):
        """Retrieve a decrypted secret."""
        if not self.password:
            raise ValueError("Master password required")
        path = self._path(name)
        if not path.exists():
            return None
        data = decrypt_config(path.read_text(), self.password)
        return data.get("value")

    def delete(self, name):
        """Delete a secret."""
        path = self._path(name)
        if path.exists():
            path.unlink()
            return True
        return False

    def list(self):
        """List all secret names."""
        return [f.stem for f in self.secrets_dir.glob("*.enc")]


# ─── CLI ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: security.py <command> [args]")
        print("Commands:")
        print("  generate-tokens     Generate NATS tokens for all agents")
        print("  generate-nats-conf  Generate NATS config with auth")
        print("  set-secret <name> <value>  Store an encrypted secret")
        print("  get-secret <name>   Retrieve a secret")
        print("  list-secrets        List all secrets")
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "generate-tokens":
        tokens = generate_agent_tokens()
        print("Generated tokens:")
        for agent, token in tokens.items():
            print(f"  {agent}: {token}")

    elif cmd == "generate-nats-conf":
        tokens = generate_agent_tokens()
        config = generate_nats_config(tokens)
        print(config)

    elif cmd == "set-secret":
        if len(sys.argv) < 4:
            print("Usage: security.py set-secret <name> <value>")
            sys.exit(1)
        import getpass
        password = getpass.getpass("Master password: ")
        sm = SecretsManager(password)
        sm.set(sys.argv[2], sys.argv[3])
        print(f"Secret '{sys.argv[2]}' saved")

    elif cmd == "get-secret":
        if len(sys.argv) < 3:
            print("Usage: security.py get-secret <name>")
            sys.exit(1)
        import getpass
        password = getpass.getpass("Master password: ")
        sm = SecretsManager(password)
        value = sm.get(sys.argv[2])
        if value:
            print(value)
        else:
            print("Secret not found")

    elif cmd == "list-secrets":
        sm = SecretsManager("")
        secrets = sm.list()
        if secrets:
            for s in secrets:
                print(f"  {s}")
        else:
            print("  No secrets stored")

    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
