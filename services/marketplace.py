#!/usr/bin/env python3
"""
Starship OS Marketplace Client

Multi-source agent skill marketplace with security scanning.
Discovers, installs, and manages agent skills from Hermes, skills.sh,
GitHub, and local directories.

Usage:
    python3 marketplace.py search "query" [--source SOURCE]
    python3 marketplace.py browse [--category CAT] [--source SOURCE]
    python3 marketplace.py info <skill-name>
    python3 marketplace.py install <skill> [--source SRC] [--force]
    python3 marketplace.py list
    python3 marketplace.py update <skill>
    python3 marketplace.py remove <skill>
    python3 marketplace.py scan <path>
    python3 marketplace.py history
"""

import sys
import os
import json
import re
import shutil
import hashlib
import time
import logging
import logging.handlers
import argparse
import base64
import urllib.request
import urllib.error
import urllib.parse
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from datetime import datetime, timezone
from typing import Any

try:
    import yaml
except ImportError:
    yaml = None

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CONFIG_PATH = Path("/etc/agnetic/marketplace.yaml")
LOCAL_SKILLS_DIR = Path(os.getenv("AGNETIC_SKILLS_DIR", str(Path(__file__).resolve().parent.parent / "skills")))
_log_dir = Path("/var/log/agnetic")
if not os.access(_log_dir, os.W_OK):
    _log_dir = Path("/tmp/agnetic-data/logs")
_log_dir.mkdir(parents=True, exist_ok=True)
LOG_DIR = _log_dir
LOG_FILE = LOG_DIR / "skill-scans.log"
HISTORY_FILE = LOCAL_SKILLS_DIR / ".marketplace-history.json"

HTTP_TIMEOUT = 30
USER_AGENT = "agnetic-marketplace/1.0"

HERMES_API = "https://hermes-agent.nousresearch.com/api/skills"
CLAWHUB_API = "https://clawhub.ai/api/skills"
SKILLS_SH_REPOS = [
    "https://github.com/anthropics/skills",
]

DEFAULT_CONFIG: dict[str, Any] = {
    "sources": {
        "hermes": {
            "enabled": True,
            "url": "https://hermes-agent.nousresearch.com",
            "trust_level": "high",
        },
        "skills_sh": {
            "enabled": True,
            "url": "https://github.com/anthropics/skills",
            "trust_level": "medium",
        },
        "github": {
            "enabled": True,
            "trust_level": "low",
        },
        "local": {
            "enabled": True,
            "trust_level": "high",
        },
    },
    "security": {
        "scan_on_install": True,
        "block_dangerous": True,
        "quarantine_dir": "/var/lib/igmatic/quarantine",
    },
}

# ---------------------------------------------------------------------------
# Security Patterns
# ---------------------------------------------------------------------------

SHELL_DANGEROUS_PATTERNS: list[tuple[str, str]] = [
    (r"\brm\s+(-[a-zA-Z]*f[a-zA-Z]*\s+|--force\s+).*-[a-zA-Z]*r", "Recursive force delete"),
    (r"\brm\s+-rf\s+/", "Delete root filesystem"),
    (r"\bmkfs\b", "Filesystem format command"),
    (r"\bdd\s+.*of=/dev/", "Direct disk write"),
    (r"\b:\(\)\s*\{", "Fork bomb"),
    (r"curl\s+.*\|\s*(ba)?sh", "Curl pipe to shell"),
    (r"wget\s+.*\|\s*(ba)?sh", "Wget pipe to shell"),
    (r"curl\s+.*\|\s*bash", "Curl pipe to bash"),
    (r"wget\s+.*\|\s*bash", "Wget pipe to bash"),
    (r"curl\s+.*\|\s*sh", "Curl pipe to sh"),
    (r"wget\s+.*\|\s*sh", "Wget pipe to sh"),
    (r"xargs\s+.*\|\s*(ba)?sh", "Xargs pipe to shell"),
]

EXFILTRATION_PATTERNS: list[tuple[str, str]] = [
    (r"curl\s+.*-d\s.*@/etc/passwd", "Sending /etc/passwd"),
    (r"wget\s+.*--post-file=/etc/", "Posting system files"),
    (r"curl\s+.*-T\s+/etc/", "Uploading system files"),
    (r"nc\s+.*-e\s+/bin/(ba)?sh", "Netcat reverse shell"),
    (r"ncat\s+.*-e\s+/bin/(ba)?sh", "Ncat reverse shell"),
    (r"python.*socket.*connect", "Python socket reverse connection"),
    (r"requests\.post\(", "HTTP POST to external server"),
    (r"urllib\.request\.urlopen\(", "HTTP request to external URL"),
    (r"subprocess\.run\(\[.*curl", "Subprocess curl call"),
]

CREDENTIAL_PATTERNS: list[tuple[str, str]] = [
    (r"cat\s+\.env\b", "Reading .env file"),
    (r"cat\s+~/\.ssh/", "Reading SSH keys"),
    (r"cat\s+/root/\.ssh/", "Reading root SSH keys"),
    (r"cat\s+~/\.aws/", "Reading AWS credentials"),
    (r"cat\s+/root/\.aws/", "Reading root AWS credentials"),
    (r"\.git/config.*https?://.*:", "Credential in git URL"),
    (r"os\.environ\[.*(?:KEY|SECRET|TOKEN|PASSWORD)", "Reading env secrets"),
    (r"open\(.*\.env\b", "Opening .env file"),
    (r"open\(.*\.pem\b", "Opening PEM key file"),
    (r"open\(.*id_rsa", "Opening SSH private key"),
    (r"readline.*\.ssh", "Reading SSH directory"),
]

PRIVILEGE_ESCALATION_PATTERNS: list[tuple[str, str]] = [
    (r"\bsudo\b", "Sudo usage"),
    (r"\bsu\s+-?\s*\w+", "Switch user"),
    (r"\bchmod\s+[0-7]*7[0-7]*\s+/", "Setting world-writable on root"),
    (r"\bchown\s+.*:\s*/", "Changing ownership of root"),
    (r"\bsetuid\b", "Setting SUID bit"),
    (r"\bsetgid\b", "Setting SGID bit"),
    (r"\bcapsh\b", "Linux capabilities manipulation"),
    (r"\bcapsh\b", "Linux capabilities tool"),
]

OBFUSCATION_PATTERNS: list[tuple[str, str]] = [
    (r"base64\s+-d\s*\|\s*(ba)?sh", "Base64 decode pipe to shell"),
    (r"base64\s+--decode\s*\|\s*(ba)?sh", "Base64 decode pipe to shell"),
    (r"eval\(\s*compile\(", "Dynamic code compilation and eval"),
    (r"exec\(.*base64", "Executing base64 decoded content"),
    (r"__import__\(.+exec\b", "Dynamic import with exec"),
]

PROMPT_INJECTION_PATTERNS: list[tuple[str, str]] = [
    (r"ignore\s+(all\s+)?previous\s+instructions", "Prompt injection attempt"),
    (r"ignore\s+(all\s+)?prior\s+instructions", "Prompt injection attempt"),
    (r"disregard\s+(all\s+)?previous", "Prompt injection attempt"),
    (r"you\s+are\s+now\s+(?:a|an)\s+\w+", "Role reassignment attempt"),
    (r"system\s*prompt\s*:", "System prompt disclosure attempt"),
    (r"new\s+instructions?\s*:", "Instruction override attempt"),
    (r"forget\s+(all|everything|previous)", "Memory manipulation attempt"),
    (r"<\|im_start\|>system", "ChatML injection attempt"),
    (r"\[INST\]\s*", "LLaMA format injection attempt"),
]

ALL_SECURITY_RULES: list[tuple[list[tuple[str, str]], str]] = [
    (SHELL_DANGEROUS_PATTERNS, "malicious_shell"),
    (EXFILTRATION_PATTERNS, "exfiltration"),
    (CREDENTIAL_PATTERNS, "credential_harvesting"),
    (PRIVILEGE_ESCALATION_PATTERNS, "privilege_escalation"),
    (OBFUSCATION_PATTERNS, "obfuscation"),
    (PROMPT_INJECTION_PATTERNS, "prompt_injection"),
]

# ---------------------------------------------------------------------------
# Data Types
# ---------------------------------------------------------------------------


class Severity(Enum):
    SAFE = "SAFE"
    WARNING = "WARNING"
    DANGEROUS = "DANGEROUS"


@dataclass
class ScanResult:
    severity: Severity
    findings: list[dict[str, str]]
    scanned_files: int
    scan_duration_ms: float
    details: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity.value,
            "findings": self.findings,
            "scanned_files": self.scanned_files,
            "scan_duration_ms": round(self.scan_duration_ms, 1),
            "details": self.details,
        }


@dataclass
class SkillResult:
    name: str
    source: str
    description: str
    stars: int = 0
    downloads: int = 0
    url: str = ""
    verified: bool = False
    category: str = ""
    version: str = ""
    author: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class InstalledSkill:
    name: str
    source: str
    installed_at: str
    version: str = ""
    url: str = ""
    scan_result: str = "SAFE"
    files: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Structured Logger
# ---------------------------------------------------------------------------


class JSONFormatter(logging.Formatter):
    """Emit log records as single-line JSON objects."""

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "service": getattr(record, "service", "marketplace"),
            "event": getattr(record, "event", record.getMessage()),
        }
        details = getattr(record, "details", None)
        if details:
            entry["details"] = details
        return json.dumps(entry, default=str)


def setup_logging() -> logging.Logger:
    logger = logging.getLogger("marketplace")
    logger.setLevel(logging.INFO)

    fmt = JSONFormatter()

    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            str(LOG_FILE), maxBytes=5 * 1024 * 1024, backupCount=5
        )
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except OSError:
        pass

    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    return logger


log = setup_logging()


def _log(event: str, level: str = "info", details: dict | None = None):
    extra: dict[str, Any] = {"event": event}
    if details:
        extra["details"] = details
    getattr(log, level, log.info)(event, extra=extra)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def load_config() -> dict[str, Any]:
    merged = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists() and yaml is not None:
        try:
            with open(CONFIG_PATH) as f:
                cfg = yaml.safe_load(f) or {}
            _log("config_loaded", details={"path": str(CONFIG_PATH)})
            if "sources" in cfg:
                for name, src_cfg in cfg["sources"].items():
                    if name in merged["sources"]:
                        merged["sources"][name] = {**merged["sources"][name], **src_cfg}
                    else:
                        merged["sources"][name] = src_cfg
            if "security" in cfg:
                merged["security"] = {**merged["security"], **cfg["security"]}
        except Exception as exc:
            _log("config_load_failed", level="warning", details={"error": str(exc)})
    return merged


# ---------------------------------------------------------------------------
# HTTP Helpers
# ---------------------------------------------------------------------------


def _http_get(url: str, timeout: int = HTTP_TIMEOUT) -> dict | list | str | None:
    """Perform an HTTP GET and return parsed JSON or text."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
            content_type = resp.headers.get("Content-Type", "")
            if "json" in content_type:
                return json.loads(data)
            return data.decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        _log("http_error", level="warning", details={"url": url, "status": exc.code})
        return None
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        _log("http_failed", level="warning", details={"url": url, "error": str(exc)})
        return None


def _http_get_raw(url: str, timeout: int = HTTP_TIMEOUT) -> bytes | None:
    """Perform an HTTP GET and return raw bytes."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, TimeoutError) as exc:
        _log("http_raw_failed", level="warning", details={"url": url, "error": str(exc)})
        return None


def _github_api(path: str) -> dict | list | None:
    """Hit the GitHub API. No token required for public repos."""
    base = "https://api.github.com"
    url = f"{base}{path}"
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/vnd.github.v3+json",
    })
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            return json.loads(resp.read())
    except (urllib.error.HTTPError, urllib.error.URLError, OSError) as exc:
        _log("github_api_error", level="warning", details={"path": path, "error": str(exc)})
        return None


def _github_raw(owner: str, repo: str, path: str, ref: str = "main") -> str | None:
    """Fetch a raw file from GitHub."""
    url = f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{path}"
    data = _http_get_raw(url)
    if data is not None:
        return data.decode("utf-8", errors="replace")
    return None


# ---------------------------------------------------------------------------
# Security Scanner
# ---------------------------------------------------------------------------


class SecurityScanner:
    """Scans skill content for malicious, exfiltrating, or dangerous patterns."""

    def __init__(self, quarantine_dir: str = "/var/lib/igmatic/quarantine"):
        self.quarantine_dir = Path(quarantine_dir)

    def scan_text(self, content: str, filename: str = "<unknown>") -> list[dict[str, str]]:
        """Scan a text blob for known bad patterns. Returns list of findings."""
        findings: list[dict[str, str]] = []
        for pattern_list, category in ALL_SECURITY_RULES:
            for pattern, desc in pattern_list:
                for match in re.finditer(pattern, content, re.IGNORECASE):
                    findings.append({
                        "category": category,
                        "description": desc,
                        "file": filename,
                        "line": self._get_line_number(content, match.start()),
                        "match": match.group()[:80],
                    })
        return findings

    def scan_skill_content(self, skill_text: str, filename: str = "SKILL.md") -> ScanResult:
        """Full scan of a skill's SKILL.md content."""
        start = time.monotonic()
        findings = self.scan_text(skill_text, filename)
        duration_ms = (time.monotonic() - start) * 1000

        severity = Severity.SAFE
        if findings:
            categories = {f["category"] for f in findings}
            # Any obfuscation, exfiltration, credential harvesting, or prompt injection is DANGEROUS
            dangerous_cats = {"obfuscation", "exfiltration", "credential_harvesting", "prompt_injection"}
            if categories & dangerous_cats:
                severity = Severity.DANGEROUS
            else:
                severity = Severity.WARNING

        return ScanResult(
            severity=severity,
            findings=findings,
            scanned_files=1,
            scan_duration_ms=duration_ms,
        )

    def scan_directory(self, skill_dir: Path) -> ScanResult:
        """Scan all files in a skill directory."""
        start = time.monotonic()
        all_findings: list[dict[str, str]] = []
        file_count = 0

        if not skill_dir.exists():
            return ScanResult(
                severity=Severity.WARNING,
                findings=[{"category": "error", "description": "Directory not found", "file": str(skill_dir), "line": "0", "match": ""}],
                scanned_files=0,
                scan_duration_ms=(time.monotonic() - start) * 1000,
            )

        scan_extensions = {".md", ".txt", ".sh", ".py", ".js", ".ts", ".yaml", ".yml", ".toml", ".json"}
        for fpath in sorted(skill_dir.rglob("*")):
            if not fpath.is_file():
                continue
            if fpath.suffix.lower() not in scan_extensions:
                continue
            if fpath.stat().st_size > 1_000_000:  # skip files > 1MB
                continue

            try:
                content = fpath.read_text(errors="ignore")
                file_count += 1
                rel = str(fpath.relative_to(skill_dir))
                all_findings.extend(self.scan_text(content, rel))
            except (PermissionError, OSError):
                continue

        duration_ms = (time.monotonic() - start) * 1000

        severity = Severity.SAFE
        if all_findings:
            categories = {f["category"] for f in all_findings}
            dangerous_cats = {"obfuscation", "exfiltration", "credential_harvesting", "prompt_injection"}
            if categories & dangerous_cats:
                severity = Severity.DANGEROUS
            else:
                severity = Severity.WARNING

        return ScanResult(
            severity=severity,
            findings=all_findings,
            scanned_files=file_count,
            scan_duration_ms=duration_ms,
        )

    def quarantine(self, skill_name: str, content: str, reason: str) -> Path:
        """Move skill content to quarantine directory."""
        self.quarantine_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        qpath = self.quarantine_dir / f"{skill_name}_{ts}.quarantine"
        try:
            qpath.write_text(content)
            _log("skill_quarantined", details={"name": skill_name, "path": str(qpath), "reason": reason})
        except OSError as exc:
            _log("quarantine_failed", level="error", details={"name": skill_name, "error": str(exc)})
        return qpath

    @staticmethod
    def _get_line_number(text: str, pos: int) -> str:
        return str(text[:pos].count("\n") + 1)


# ---------------------------------------------------------------------------
# Source Parsers
# ---------------------------------------------------------------------------


class HermesSource:
    """Skills from Hermes Agent Skills Hub / agentskills.io standard."""

    name = "hermes"

    def search(self, query: str) -> list[SkillResult]:
        results: list[SkillResult] = []

        # Try the primary API
        data = _http_get(f"{HERMES_API}?q={urllib.parse.quote(query)}")
        if isinstance(data, list):
            for item in data:
                results.append(SkillResult(
                    name=item.get("name", "unknown"),
                    source=self.name,
                    description=item.get("description", ""),
                    stars=item.get("stars", 0),
                    downloads=item.get("downloads", 0),
                    url=item.get("url", ""),
                    verified=item.get("verified", False),
                    category=item.get("category", ""),
                    version=item.get("version", ""),
                    author=item.get("author", ""),
                ))
            return results

        # Fallback: search ClawHub
        data = _http_get(f"{CLAWHUB_API}?q={urllib.parse.quote(query)}")
        if isinstance(data, list):
            for item in data:
                results.append(SkillResult(
                    name=item.get("name", "unknown"),
                    source=self.name,
                    description=item.get("description", ""),
                    url=item.get("url", ""),
                ))

        return results

    def browse(self, category: str = "") -> list[SkillResult]:
        results: list[SkillResult] = []
        url = HERMES_API
        if category:
            url += f"?category={urllib.parse.quote(category)}"
        data = _http_get(url)
        if isinstance(data, list):
            for item in data:
                results.append(SkillResult(
                    name=item.get("name", "unknown"),
                    source=self.name,
                    description=item.get("description", ""),
                    stars=item.get("stars", 0),
                    downloads=item.get("downloads", 0),
                    url=item.get("url", ""),
                    verified=item.get("verified", False),
                    category=item.get("category", ""),
                    version=item.get("version", ""),
                    author=item.get("author", ""),
                ))
        return results

    def get_skill(self, name: str) -> dict[str, str] | None:
        """Fetch SKILL.md content for a named skill."""
        data = _http_get(f"{HERMES_API}/{urllib.parse.quote(name)}")
        if isinstance(data, dict):
            return {
                "skill_md": data.get("content", data.get("skill_md", "")),
                "name": data.get("name", name),
                "url": data.get("url", ""),
            }
        return None


class SkillsShSource:
    """Skills from skills.sh / Vercel GitHub-based registries."""

    name = "skills_sh"

    def search(self, query: str) -> list[SkillResult]:
        results: list[SkillResult] = []
        query_lower = query.lower()

        for repo_url in SKILLS_SH_REPOS:
            parts = repo_url.replace("https://github.com/", "").split("/")
            if len(parts) < 2:
                continue
            owner, repo = parts[0], parts[1]

            # List repo contents
            data = _github_api(f"/repos/{owner}/{repo}/contents/")
            if not isinstance(data, list):
                continue

            for entry in data:
                if entry.get("type") != "dir":
                    continue
                dir_name = entry["name"]
                # Check if this directory has a SKILL.md
                skill_data = _github_api(f"/repos/{owner}/{repo}/contents/{dir_name}/SKILL.md")
                if not isinstance(skill_data, dict):
                    continue

                content = _fetch_github_content(skill_data)
                if not content:
                    continue

                desc = _extract_description(content)
                name = _extract_skill_name(content) or dir_name
                if query_lower not in name.lower() and query_lower not in desc.lower():
                    continue

                results.append(SkillResult(
                    name=name,
                    source=self.name,
                    description=desc,
                    url=f"{repo_url}/tree/main/{dir_name}",
                    category=_extract_category(content),
                    version=_extract_version(content),
                ))

        return results

    def browse(self, category: str = "") -> list[SkillResult]:
        results: list[SkillResult] = []
        cat_lower = category.lower()

        for repo_url in SKILLS_SH_REPOS:
            parts = repo_url.replace("https://github.com/", "").split("/")
            if len(parts) < 2:
                continue
            owner, repo = parts[0], parts[1]

            data = _github_api(f"/repos/{owner}/{repo}/contents/")
            if not isinstance(data, list):
                continue

            for entry in data:
                if entry.get("type") != "dir":
                    continue
                dir_name = entry["name"]
                skill_data = _github_api(f"/repos/{owner}/{repo}/contents/{dir_name}/SKILL.md")
                if not isinstance(skill_data, dict):
                    continue

                content = _fetch_github_content(skill_data)
                if not content:
                    continue

                desc = _extract_description(content)
                name = _extract_skill_name(content) or dir_name
                skill_cat = _extract_category(content)

                if cat_lower and cat_lower not in skill_cat.lower() and cat_lower not in desc.lower():
                    continue

                results.append(SkillResult(
                    name=name,
                    source=self.name,
                    description=desc,
                    url=f"{repo_url}/tree/main/{dir_name}",
                    category=skill_cat,
                    version=_extract_version(content),
                ))

        return results

    def get_skill(self, name: str) -> dict[str, str] | None:
        """Fetch SKILL.md by trying all known repos."""
        for repo_url in SKILLS_SH_REPOS:
            parts = repo_url.replace("https://github.com/", "").split("/")
            if len(parts) < 2:
                continue
            owner, repo = parts[0], parts[1]

            # Try the skill name as a directory
            skill_data = _github_api(f"/repos/{owner}/{repo}/contents/{name}/SKILL.md")
            if isinstance(skill_data, dict):
                content = _fetch_github_content(skill_data)
                if content:
                    return {"skill_md": content, "name": name, "url": f"{repo_url}/tree/main/{name}"}

        return None


class GitHubSource:
    """Direct GitHub repo install. Handles 'github:org/repo/path' specifiers."""

    name = "github"

    def search(self, query: str) -> list[SkillResult]:
        """Search GitHub for repos containing SKILL.md files."""
        results: list[SkillResult] = []
        data = _github_api(f"/search/repositories?q={urllib.parse.quote(query + ' SKILL.md')}&per_page=20")
        if not isinstance(data, dict):
            return results

        for item in data.get("items", []):
            full_name = item.get("full_name", "")
            desc = item.get("description", "") or ""
            results.append(SkillResult(
                name=full_name,
                source=self.name,
                description=desc,
                stars=item.get("stargazers_count", 0),
                url=item.get("html_url", ""),
                verified=False,
            ))

        return results

    def parse_spec(self, spec: str) -> tuple[str, str, str]:
        """Parse 'github:org/repo/path' into (owner, repo, path).

        Returns ("", "", "") if invalid.
        """
        spec = spec.removeprefix("github:")
        parts = spec.strip("/").split("/")
        if len(parts) < 2:
            return "", "", ""
        owner = parts[0]
        repo = parts[1]
        path = "/".join(parts[2:]) if len(parts) > 2 else ""
        return owner, repo, path

    def download_skill(self, owner: str, repo: str, path: str) -> dict[str, str] | None:
        """Download a SKILL.md and any referenced files from a GitHub repo."""
        # If path points to a SKILL.md directly
        if path.endswith("SKILL.md"):
            content = _github_raw(owner, repo, path)
            if not content:
                return None
            return {"skill_md": content, "name": Path(path).parent.name, "url": f"https://github.com/{owner}/{repo}/tree/main/{path}"}

        # If path is a directory, look for SKILL.md in it
        skill_path = f"{path}/SKILL.md" if path else "SKILL.md"
        content = _github_raw(owner, repo, skill_path)
        if not content:
            # Try without explicit path
            content = _github_raw(owner, repo, "SKILL.md")
            if not content:
                return None
            skill_path = "SKILL.md"

        name = Path(path).name if path else repo
        return {"skill_md": content, "name": name, "url": f"https://github.com/{owner}/{repo}/tree/main/{path}"}


class LocalSource:
    """Local directory or file install."""

    name = "local"

    def install_from_path(self, source_path: str) -> dict[str, str] | None:
        """Load a skill from a local path."""
        p = Path(source_path).expanduser().resolve()

        if not p.exists():
            _log("local_path_not_found", level="warning", details={"path": str(p)})
            return None

        # Direct SKILL.md file
        if p.is_file() and p.name == "SKILL.md":
            content = p.read_text(errors="ignore")
            name = p.parent.name
            return {"skill_md": content, "name": name, "url": str(p)}

        # Directory containing SKILL.md
        if p.is_dir():
            skill_file = p / "SKILL.md"
            if skill_file.exists():
                content = skill_file.read_text(errors="ignore")
                return {"skill_md": content, "name": p.name, "url": str(p)}

        _log("no_skill_md_found", level="warning", details={"path": str(p)})
        return None

    def search(self, query: str) -> list[SkillResult]:
        """Search local skills directory."""
        results: list[SkillResult] = []
        query_lower = query.lower()

        if not LOCAL_SKILLS_DIR.exists():
            return results

        for skill_dir in LOCAL_SKILLS_DIR.iterdir():
            if not skill_dir.is_dir():
                continue
            skill_file = skill_dir / "SKILL.md"
            if not skill_file.exists():
                continue

            try:
                content = skill_file.read_text(errors="ignore")
            except OSError:
                continue

            name = _extract_skill_name(content) or skill_dir.name
            desc = _extract_description(content)
            if query_lower in name.lower() or query_lower in desc.lower():
                results.append(SkillResult(
                    name=name,
                    source="local",
                    description=desc,
                    url=str(skill_file),
                ))

        return results

    def browse(self, category: str = "") -> list[SkillResult]:
        """List all local skills, optionally filtered by category."""
        results: list[SkillResult] = []
        cat_lower = category.lower()

        if not LOCAL_SKILLS_DIR.exists():
            return results

        for skill_dir in LOCAL_SKILLS_DIR.iterdir():
            if not skill_dir.is_dir():
                continue
            skill_file = skill_dir / "SKILL.md"
            if not skill_file.exists():
                continue

            try:
                content = skill_file.read_text(errors="ignore")
            except OSError:
                continue

            name = _extract_skill_name(content) or skill_dir.name
            desc = _extract_description(content)
            skill_cat = _extract_category(content)

            if cat_lower and cat_lower not in skill_cat.lower() and cat_lower not in desc.lower():
                continue

            results.append(SkillResult(
                name=name,
                source="local",
                description=desc,
                url=str(skill_file),
                category=skill_cat,
            ))

        return results


# ---------------------------------------------------------------------------
# SKILL.md Helpers
# ---------------------------------------------------------------------------


def _extract_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Extract YAML frontmatter from SKILL.md. Returns (metadata, body)."""
    metadata: dict[str, str] = {}
    body = text

    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            fm_text = parts[1].strip()
            body = parts[2].strip()
            if yaml is not None:
                try:
                    parsed = yaml.safe_load(fm_text)
                    if isinstance(parsed, dict):
                        metadata = {str(k): str(v) for k, v in parsed.items()}
                except Exception:
                    pass
            else:
                # Minimal key: value parser
                for line in fm_text.split("\n"):
                    if ":" in line:
                        key, _, value = line.partition(":")
                        metadata[key.strip()] = value.strip()

    return metadata, body


def _extract_skill_name(text: str) -> str:
    """Extract skill name from SKILL.md heading or frontmatter."""
    meta, body = _extract_frontmatter(text)
    if "name" in meta:
        return meta["name"]
    # First heading
    for line in body.split("\n"):
        line = line.strip()
        if line.startswith("# "):
            return line[2:].strip()
    return ""


def _extract_description(text: str) -> str:
    """Extract description from frontmatter or first paragraph."""
    meta, body = _extract_frontmatter(text)
    if "description" in meta:
        return meta["description"]
    # First non-empty, non-heading line
    for line in body.split("\n"):
        line = line.strip()
        if line and not line.startswith("#") and not line.startswith("---"):
            return line
    return ""


def _extract_category(text: str) -> str:
    meta, _ = _extract_frontmatter(text)
    return meta.get("category", meta.get("tags", ""))


def _extract_version(text: str) -> str:
    meta, _ = _extract_frontmatter(text)
    return meta.get("version", "")


def _fetch_github_content(api_data: dict) -> str | None:
    """Decode a file from GitHub API response (base64 encoded content)."""
    content = api_data.get("content", "")
    encoding = api_data.get("encoding", "")
    if encoding == "base64" and content:
        try:
            return base64.b64decode(content).decode("utf-8", errors="replace")
        except Exception:
            return None
    # Fallback: download via raw URL
    download_url = api_data.get("download_url", "")
    if download_url:
        data = _http_get_raw(download_url)
        if data:
            return data.decode("utf-8", errors="replace")
    return None


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------


def _load_history() -> list[dict[str, Any]]:
    if HISTORY_FILE.exists():
        try:
            return json.loads(HISTORY_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            return []
    return []


def _save_history(entries: list[dict[str, Any]]):
    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        HISTORY_FILE.write_text(json.dumps(entries, indent=2, default=str))
    except OSError as exc:
        _log("history_save_failed", level="warning", details={"error": str(exc)})


def _append_history(action: str, skill_name: str, details: dict[str, Any] | None = None):
    entries = _load_history()
    entry: dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "action": action,
        "skill": skill_name,
    }
    if details:
        entry["details"] = details
    entries.append(entry)
    _save_history(entries)


def _load_installed() -> dict[str, dict]:
    """Load the installed skills registry."""
    registry_file = LOCAL_SKILLS_DIR / ".installed.json"
    if registry_file.exists():
        try:
            return json.loads(registry_file.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_installed(registry: dict[str, dict]):
    registry_file = LOCAL_SKILLS_DIR / ".installed.json"
    LOCAL_SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        registry_file.write_text(json.dumps(registry, indent=2, default=str))
    except OSError as exc:
        _log("registry_save_failed", level="warning", details={"error": str(exc)})


# ---------------------------------------------------------------------------
# Marketplace Core
# ---------------------------------------------------------------------------


class Marketplace:
    """Multi-source agent skill marketplace."""

    def __init__(self, config: dict[str, Any] | None = None):
        self.config = config or load_config()
        self.scanner = SecurityScanner(
            quarantine_dir=self.config["security"]["quarantine_dir"],
        )
        self.sources: dict[str, Any] = {}
        self._init_sources()

    def _init_sources(self):
        src_cfg = self.config["sources"]
        if src_cfg.get("hermes", {}).get("enabled"):
            self.sources["hermes"] = HermesSource()
        if src_cfg.get("skills_sh", {}).get("enabled"):
            self.sources["skills_sh"] = SkillsShSource()
        if src_cfg.get("github", {}).get("enabled"):
            self.sources["github"] = GitHubSource()
        if src_cfg.get("local", {}).get("enabled"):
            self.sources["local"] = LocalSource()

    def search(self, query: str, sources: list[str] | None = None) -> list[SkillResult]:
        """Search across enabled sources."""
        active = sources or list(self.sources.keys())
        results: list[SkillResult] = []
        for src_name in active:
            src = self.sources.get(src_name)
            if src is None:
                continue
            try:
                results.extend(src.search(query))
            except Exception as exc:
                _log("search_failed", level="warning", details={"source": src_name, "error": str(exc)})
        return results

    def browse(self, category: str = "", source: str = "") -> list[SkillResult]:
        """Browse skills by category."""
        active = [source] if source else list(self.sources.keys())
        results: list[SkillResult] = []
        for src_name in active:
            src = self.sources.get(src_name)
            if src is None:
                continue
            try:
                results.extend(src.browse(category))
            except Exception as exc:
                _log("browse_failed", level="warning", details={"source": src_name, "error": str(exc)})
        return results

    def info(self, name: str) -> SkillResult | None:
        """Get detailed info about a skill by name."""
        results = self.search(name)
        for r in results:
            if r.name.lower() == name.lower():
                return r
        # Partial match
        for r in results:
            if name.lower() in r.name.lower():
                return r
        return None

    def install(self, name: str, source: str = "", force: bool = False) -> bool:
        """Download, scan, and install a skill.

        Handles:
          - Plain skill names (search sources)
          - github:org/repo/path specifiers
          - Local /path/to/skill
        """
        # --- Local path install ---
        if name.startswith("/") or name.startswith("~"):
            return self._install_local(name)

        # --- GitHub spec install ---
        if name.startswith("github:"):
            return self._install_github_spec(name)

        # --- Named skill install ---
        return self._install_named(name, source, force)

    def _install_local(self, path: str) -> bool:
        local_src = self.sources.get("local")
        if not local_src:
            _log("local_source_disabled", level="error")
            print("Error: local source is disabled in config")
            return False

        skill_data = local_src.install_from_path(path)
        if not skill_data:
            print(f"Error: could not load skill from {path}")
            return False

        return self._finish_install(skill_data, "local", force=True)

    def _install_github_spec(self, spec: str) -> bool:
        gh_src = self.sources.get("github")
        if not gh_src:
            _log("github_source_disabled", level="error")
            print("Error: github source is disabled in config")
            return False

        owner, repo, path = gh_src.parse_spec(spec)
        if not owner or not repo:
            print(f"Error: invalid github spec '{spec}'. Expected github:org/repo/path")
            return False

        skill_data = gh_src.download_skill(owner, repo, path)
        if not skill_data:
            print(f"Error: could not download skill from {spec}")
            return False

        return self._finish_install(skill_data, "github", force=False)

    def _install_named(self, name: str, source: str, force: bool) -> bool:
        # Try the specified source first, then all sources
        sources_to_try = [source] if source else list(self.sources.keys())
        skill_data = None
        found_source = ""

        for src_name in sources_to_try:
            src = self.sources.get(src_name)
            if src is None:
                continue
            try:
                data = src.get_skill(name)
                if data:
                    skill_data = data
                    found_source = src_name
                    break
            except Exception as exc:
                _log("fetch_failed", level="warning", details={"source": src_name, "name": name, "error": str(exc)})

        if not skill_data:
            print(f"Error: skill '{name}' not found in {sources_to_try}")
            return False

        return self._finish_install(skill_data, found_source, force)

    def _finish_install(self, skill_data: dict[str, str], source: str, force: bool) -> bool:
        skill_md = skill_data.get("skill_md", "")
        skill_name = skill_data.get("name", "unknown")
        skill_url = skill_data.get("url", "")

        if not skill_md:
            print(f"Error: empty skill content for '{skill_name}'")
            return False

        # --- Security scan ---
        scan_on_install = self.config["security"].get("scan_on_install", True)
        if scan_on_install:
            scan_result = self.scanner.scan_skill_content(skill_md)
            self._log_scan(skill_name, source, scan_result)

            if scan_result.severity == Severity.DANGEROUS:
                if force:
                    print(f"WARNING: '{skill_name}' rated DANGEROUS ({len(scan_result.findings)} findings)")
                    print("  Use --force to override. Skill will be quarantined.")
                    self.scanner.quarantine(skill_name, skill_md, "dangerous_override")
                    _append_history("install_blocked", skill_name, {"reason": "dangerous", "source": source, "forced": True})
                else:
                    print(f"BLOCKED: '{skill_name}' rated DANGEROUS ({len(scan_result.findings)} findings)")
                    print("  Re-run with --force to install anyway (skill will be quarantined).")
                    for f in scan_result.findings[:5]:
                        print(f"    [{f['category']}] {f['description']} (line {f['line']})")
                    _append_history("install_blocked", skill_name, {"reason": "dangerous", "source": source})
                    return False

            if scan_result.severity == Severity.WARNING:
                print(f"WARNING: '{skill_name}' has {len(scan_result.findings)} security finding(s):")
                for f in scan_result.findings[:5]:
                    print(f"    [{f['category']}] {f['description']} (line {f['line']})")
                if not force:
                    confirm = input("  Continue installation? [y/N] ").strip().lower()
                    if confirm not in ("y", "yes"):
                        print("Installation cancelled.")
                        _append_history("install_cancelled", skill_name, {"reason": "warning", "source": source})
                        return False

        # --- Write to skills directory ---
        skill_dir = LOCAL_SKILLS_DIR / skill_name
        try:
            skill_dir.mkdir(parents=True, exist_ok=True)
            skill_file = skill_dir / "SKILL.md"
            skill_file.write_text(skill_md)
        except OSError as exc:
            print(f"Error: could not write skill files: {exc}")
            _log("install_write_failed", level="error", details={"name": skill_name, "error": str(exc)})
            return False

        # --- Update registry ---
        registry = _load_installed()
        scan_label = "SAFE"
        if scan_on_install:
            scan_label = self.scanner.scan_skill_content(skill_md).severity.value
        registry[skill_name] = {
            "name": skill_name,
            "source": source,
            "installed_at": datetime.now(timezone.utc).isoformat(),
            "url": skill_url,
            "scan_result": scan_label,
            "files": ["SKILL.md"],
        }
        _save_installed(registry)

        _append_history("install", skill_name, {"source": source, "url": skill_url})
        _log("skill_installed", details={"name": skill_name, "source": source, "path": str(skill_dir)})
        print(f"Installed '{skill_name}' to {skill_dir}")
        return True

    def update(self, name: str) -> bool:
        """Re-download and replace an installed skill."""
        registry = _load_installed()
        entry = registry.get(name)
        if not entry:
            print(f"Error: '{name}' is not installed")
            return False

        source = entry.get("source", "")
        url = entry.get("url", "")

        _log("updating_skill", details={"name": name, "source": source})

        # Remove existing
        skill_dir = LOCAL_SKILLS_DIR / name
        if skill_dir.exists():
            shutil.rmtree(skill_dir)

        # Re-install
        if source == "local":
            if url:
                return self._install_local(url)
            print(f"Error: cannot update local skill '{name}' without original path")
            return False
        elif source == "github" and url:
            # Parse the URL back to a github spec
            # e.g. https://github.com/org/repo/tree/main/path
            spec = url.replace("https://github.com/", "github:").replace("/tree/main/", "/")
            return self._install_github_spec(spec)
        else:
            return self._install_named(name, source, force=True)

    def remove(self, name: str) -> bool:
        """Remove an installed skill."""
        skill_dir = LOCAL_SKILLS_DIR / name
        if not skill_dir.exists():
            print(f"Error: '{name}' is not installed")
            return False

        try:
            shutil.rmtree(skill_dir)
        except OSError as exc:
            print(f"Error: could not remove '{name}': {exc}")
            return False

        registry = _load_installed()
        if name in registry:
            del registry[name]
            _save_installed(registry)

        _append_history("remove", name)
        _log("skill_removed", details={"name": name})
        print(f"Removed '{name}'")
        return True

    def list_installed(self) -> list[dict[str, Any]]:
        """List all installed skills."""
        registry = _load_installed()
        return list(registry.values())

    def history(self) -> list[dict[str, Any]]:
        """Return install/update/remove history."""
        return _load_history()

    def scan(self, path: str) -> ScanResult:
        """Scan a local skill directory or file for security issues."""
        p = Path(path).expanduser().resolve()
        if p.is_file():
            content = p.read_text(errors="ignore")
            return self.scanner.scan_skill_content(content, p.name)
        elif p.is_dir():
            return self.scanner.scan_directory(p)
        else:
            return ScanResult(
                severity=Severity.WARNING,
                findings=[{"category": "error", "description": "Path not found", "file": path, "line": "0", "match": ""}],
                scanned_files=0,
                scan_duration_ms=0,
            )

    def _log_scan(self, skill_name: str, source: str, result: ScanResult):
        """Log scan results."""
        _log("skill_scanned", details={
            "name": skill_name,
            "source": source,
            "severity": result.severity.value,
            "findings_count": len(result.findings),
            "scanned_files": result.scanned_files,
            "duration_ms": result.scan_duration_ms,
        })


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _fmt_skill_table(results: list[SkillResult], compact: bool = False):
    """Print skills in a formatted table."""
    if not results:
        print("No results found.")
        return

    if compact:
        for r in results:
            stars_str = f"  {r.stars}*" if r.stars else ""
            dl_str = f"  {r.downloads}dl" if r.downloads else ""
            verified = " [verified]" if r.verified else ""
            print(f"  {r.name:<30} {r.source:<12}{stars_str}{dl_str}{verified}")
            if r.description:
                print(f"    {r.description[:80]}")
    else:
        print(f"\n{'Name':<30} {'Source':<12} {'Stars':<8} {'DLs':<8} {'Verified':<10}")
        print("-" * 72)
        for r in results:
            print(f"{r.name:<30} {r.source:<12} {r.stars:<8} {r.downloads:<8} {'Yes' if r.verified else 'No':<10}")
            if r.description:
                print(f"  {r.description[:70]}")
        print()


def _fmt_history(entries: list[dict[str, Any]]):
    """Print history entries."""
    if not entries:
        print("No history found.")
        return
    print(f"\n{'Timestamp':<28} {'Action':<18} {'Skill':<30}")
    print("-" * 78)
    for e in entries:
        ts = e.get("timestamp", "")[:19]
        action = e.get("action", "")
        skill = e.get("skill", "")
        print(f"{ts:<28} {action:<18} {skill:<30}")
    print()


def cmd_search(args, marketplace: Marketplace):
    results = marketplace.search(args.query, sources=args.source)
    _fmt_skill_table(results)
    return 0


def cmd_browse(args, marketplace: Marketplace):
    results = marketplace.browse(category=args.category or "", source=args.source or "")
    _fmt_skill_table(results)
    return 0


def cmd_info(args, marketplace: Marketplace):
    result = marketplace.info(args.skill)
    if not result:
        print(f"Skill '{args.skill}' not found.")
        return 1

    print(f"\n  Name:        {result.name}")
    print(f"  Source:      {result.source}")
    print(f"  Description: {result.description}")
    if result.version:
        print(f"  Version:     {result.version}")
    if result.author:
        print(f"  Author:      {result.author}")
    if result.stars:
        print(f"  Stars:       {result.stars}")
    if result.downloads:
        print(f"  Downloads:   {result.downloads}")
    if result.category:
        print(f"  Category:    {result.category}")
    if result.url:
        print(f"  URL:         {result.url}")
    print(f"  Verified:    {'Yes' if result.verified else 'No'}")
    print()
    return 0


def cmd_install(args, marketplace: Marketplace):
    ok = marketplace.install(args.skill, source=args.source or "", force=args.force)
    return 0 if ok else 1


def cmd_list(args, marketplace: Marketplace):
    installed = marketplace.list_installed()
    if not installed:
        print("No skills installed.")
        return 0

    print(f"\n{'Name':<30} {'Source':<12} {'Installed':<22} {'Scan':<10}")
    print("-" * 78)
    for entry in installed:
        ts = entry.get("installed_at", "")[:19]
        print(f"{entry.get('name', '?'):<30} {entry.get('source', '?'):<12} {ts:<22} {entry.get('scan_result', '?'):<10}")
    print()
    return 0


def cmd_update(args, marketplace: Marketplace):
    ok = marketplace.update(args.skill)
    return 0 if ok else 1


def cmd_remove(args, marketplace: Marketplace):
    ok = marketplace.remove(args.skill)
    return 0 if ok else 1


def cmd_scan(args, marketplace: Marketplace):
    result = marketplace.scan(args.path)
    print(f"\n  Severity:  {result.severity.value}")
    print(f"  Files:     {result.scanned_files}")
    print(f"  Duration:  {result.scan_duration_ms:.1f}ms")
    if result.findings:
        print(f"  Findings:  {len(result.findings)}")
        for f in result.findings:
            print(f"    [{f['category']}] {f['description']}")
            print(f"      File: {f['file']}:{f['line']}  Match: {f['match']}")
    else:
        print("  Findings:  none")
    print()
    return 0 if result.severity == Severity.SAFE else 1


def cmd_history(args, marketplace: Marketplace):
    entries = marketplace.history()
    _fmt_history(entries)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="marketplace",
        description="Starship OS Agent Skill Marketplace",
    )
    sub = parser.add_subparsers(dest="command", help="Available commands")

    # search
    p_search = sub.add_parser("search", help="Search for skills across sources")
    p_search.add_argument("query", help="Search query")
    p_search.add_argument("--source", "-s", help="Limit to specific source")

    # browse
    p_browse = sub.add_parser("browse", help="Browse skills by category")
    p_browse.add_argument("--category", "-c", help="Category to browse")
    p_browse.add_argument("--source", "-s", help="Limit to specific source")

    # info
    p_info = sub.add_parser("info", help="Show skill details")
    p_info.add_argument("skill", help="Skill name")

    # install
    p_install = sub.add_parser("install", help="Install a skill")
    p_install.add_argument("skill", help="Skill name, github:org/repo/path, or /local/path")
    p_install.add_argument("--source", "-s", default="", help="Preferred source")
    p_install.add_argument("--force", "-f", action="store_true", help="Force install even if dangerous")

    # list
    sub.add_parser("list", help="List installed skills")

    # update
    p_update = sub.add_parser("update", help="Update an installed skill")
    p_update.add_argument("skill", help="Skill name")

    # remove
    p_remove = sub.add_parser("remove", help="Remove an installed skill")
    p_remove.add_argument("skill", help="Skill name")

    # scan
    p_scan = sub.add_parser("scan", help="Scan a skill for security issues")
    p_scan.add_argument("path", help="Path to skill directory or SKILL.md file")

    # history
    sub.add_parser("history", help="Show install/update/remove history")

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return 1

    config = load_config()
    marketplace = Marketplace(config)

    dispatch = {
        "search": cmd_search,
        "browse": cmd_browse,
        "info": cmd_info,
        "install": cmd_install,
        "list": cmd_list,
        "update": cmd_update,
        "remove": cmd_remove,
        "scan": cmd_scan,
        "history": cmd_history,
    }

    handler = dispatch.get(args.command)
    if handler is None:
        parser.print_help()
        return 1

    return handler(args, marketplace)


if __name__ == "__main__":
    sys.exit(main())
