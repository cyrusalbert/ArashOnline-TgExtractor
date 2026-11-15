"""Configuration loading, shared by the GitHub Actions and the local workflow.

Precedence (lowest -> highest):
  1. DEFAULTS below      - so a partial or broken config.yaml never crashes a run
  2. config.yaml
  3. profiles.<name>     - "ci" on GitHub Actions, "local" on your own PC
  4. environment         - V2S__section__key=value   (value is parsed as YAML)
"""
from __future__ import annotations

import copy
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

log = logging.getLogger("v2s.config")

ENV_PREFIX = "V2S__"

DEFAULTS: Dict[str, Any] = {
    "channels_file": "channels.txt",
    "scraper": {
        "mode": "web",
        "messages_per_channel": 100,
        "base_url": "https://t.me/s",
        "request_timeout": 20,
        "retries": 3,
        "delay_between_requests": 1.0,
        "max_pages_per_channel": 15,
        "proxy": "",
        "user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        ),
    },
    "parser": {"max_link_length": 4096},
    "core": {"path": "", "version": "latest", "fallback_version": "1.12.4", "bin_dir": "bin"},
    "testing": {
        "max_configs": 600,
        "concurrency": 40,
        "speed_concurrency": 3,
        "startup_timeout": 6,
        "max_runtime_minutes": 0,
        "ping": {
            "url": "http://cp.cloudflare.com/generate_204",
            "expected_status": 204,
            "warmup": 1,
            "attempts": 3,
            "min_success": 2,
            "timeout": 6,
            "max_ping_ms": 3000,
        },
        "geo": {
            "enabled": True,
            "timeout": 8,
            "providers": ["ip-api", "ipwho", "ipinfo", "country-is"],
        },
        "speed": {
            "enabled": True,
            "urls": [
                "https://speed.cloudflare.com/__down?bytes=10000000",
                "http://cachefly.cachefly.net/10mb.test",
                "https://proof.ovh.net/files/10Mb.dat",
            ],
            "timeout": 15,
            "connect_timeout": 8,
            "top_n": 150,
            "min_mbps": 0,
            "drop_on_fail": True,
        },
    },
    "rename": {"template": "{flag} {cc} | {ping}ms | {speed}Mbps | #{rank:03d}"},
    "output": {
        "dir": ".",
        "top_n": 0,
        "base64_subscriptions": True,
        "results_json": True,
        "keep_old_if_empty": True,
        "commit_message": "chore: update configs ({utc})",
    },
    "logging": {"level": "INFO", "file": ""},
}


def deep_merge(base: Dict[str, Any], override: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Return a new dict: `base` with `override` merged in recursively."""
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def load_dotenv(path: Path) -> None:
    """Tiny .env reader (KEY=VALUE per line) so local runs can keep secrets out of the shell.

    Existing environment variables always win. Missing file is fine.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def apply_env_overrides(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Apply V2S__section__key=value overrides. Empty values are ignored on purpose
    (GitHub Actions passes unset workflow inputs as empty strings)."""
    for name, raw in os.environ.items():
        if not name.upper().startswith(ENV_PREFIX) or not raw.strip():
            continue
        path = name[len(ENV_PREFIX):].lower().split("__")
        try:
            value = yaml.safe_load(raw)
        except yaml.YAMLError:
            value = raw
        node = cfg
        for part in path[:-1]:
            nxt = node.get(part)
            if not isinstance(nxt, dict):
                nxt = node[part] = {}
            node = nxt
        node[path[-1]] = value
        log.info("Config override from environment: %s", ".".join(path))
    return cfg


def load_config(path: Path, profile: Optional[str] = None) -> Dict[str, Any]:
    """Load config.yaml (+ profile + env overrides) on top of the built-in defaults."""
    user: Dict[str, Any] = {}
    if path.is_file():
        try:
            loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            if isinstance(loaded, dict):
                user = loaded
            else:
                log.error("%s must contain a mapping at top level - using defaults", path)
        except (OSError, yaml.YAMLError) as exc:
            log.error("Cannot read %s (%s) - using defaults", path, exc)
    else:
        log.warning("%s not found - using built-in defaults", path)

    profiles = user.pop("profiles", None) or {}
    cfg = deep_merge(DEFAULTS, user)
    if profile:
        if profile in profiles:
            cfg = deep_merge(cfg, profiles[profile])
        else:
            log.warning("Profile %r not found in %s", profile, path)
    cfg = apply_env_overrides(cfg)
    cfg["_profile"] = profile or ""
    return cfg
