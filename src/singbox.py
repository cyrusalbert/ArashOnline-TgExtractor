"""sing-box integration: build an outbound from a ProxyConfig and run it as a local proxy.

Why sing-box (and not Xray-core)?  It speaks *all six* protocols the pipeline
accepts - vmess, vless, trojan, shadowsocks, hysteria2 and tuic - with one binary.
Xray-core has no TUIC and, depending on version, no Hysteria2.

Isolation model: ONE core process per tested config, listening on a random free
127.0.0.1 port. A config that crashes the core, hangs, or is rejected only affects
its own test. Requires sing-box >= 1.11 (rule `action` syntax).

Transports sing-box cannot do (mKCP, QUIC, XHTTP/SplitHTTP, VLESS post-quantum
encryption, exotic SS plugins) raise UnsupportedConfig: the link is still valid and
stays in raw_links.txt, it just cannot be tested here.
"""
from __future__ import annotations

import atexit
import json
import logging
import os
import re
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional, Set
from urllib.parse import parse_qsl, urlencode

from .models import ProxyConfig

log = logging.getLogger("v2s.singbox")


class UnsupportedConfig(Exception):
    """Valid link, but sing-box has no equivalent for part of it."""


class CoreError(RuntimeError):
    """The core process could not be started or rejected the generated config."""


# --------------------------------------------------------------------------- #
# Outbound builders                                                           #
# --------------------------------------------------------------------------- #

_UTLS = {"chrome", "firefox", "edge", "safari", "360", "qq", "ios", "android", "random", "randomized"}
_SS_PLUGINS = {"", "obfs-local", "v2ray-plugin"}


def _tls(p: Dict[str, Any], host: str, *, force: bool = False,
         default_alpn: Optional[list] = None) -> Optional[Dict[str, Any]]:
    """TLS / REALITY block, or None for plaintext transports."""
    security = p.get("security", "none")
    if security == "none" and not force:
        return None
    first_host_header = str(p.get("host") or "").split(",")[0].strip()
    tls: Dict[str, Any] = {"enabled": True, "server_name": p.get("sni") or first_host_header or host}
    if p.get("allow_insecure"):
        tls["insecure"] = True
    alpn = p.get("alpn") or default_alpn
    if alpn:
        tls["alpn"] = list(alpn)
    fingerprint = str(p.get("fp") or "").lower()
    if security == "reality":
        tls["reality"] = {"enabled": True, "public_key": p["pbk"], "short_id": p.get("sid", "")}
        tls["utls"] = {"enabled": True, "fingerprint": fingerprint if fingerprint in _UTLS else "chrome"}
    elif fingerprint in _UTLS:
        tls["utls"] = {"enabled": True, "fingerprint": fingerprint}
    return tls


def _transport(p: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """V2Ray-style transport block for vmess/vless/trojan."""
    network = p.get("network", "tcp")
    path = p.get("path") or "/"
    host = str(p.get("host") or "")
    if network == "tcp":
        if p.get("header_type") == "http":  # TCP + HTTP header obfuscation
            return {"type": "http", "host": [h.strip() for h in host.split(",") if h.strip()], "path": path}
        return None
    if network == "ws":
        transport: Dict[str, Any] = {"type": "ws"}
        base, _, query = path.partition("?")
        if query:  # Xray-style "?ed=2048" early data lives in the path
            pairs = parse_qsl(query, keep_blank_values=True)
            early = next((v for k, v in pairs if k == "ed"), "")
            rest = [(k, v) for k, v in pairs if k != "ed"]
            if early.isdigit():
                transport["max_early_data"] = int(early)
                transport["early_data_header_name"] = "Sec-WebSocket-Protocol"
            path = base + (("?" + urlencode(rest)) if rest else "")
        transport["path"] = path
        if host:
            transport["headers"] = {"Host": host}
        return transport
    if network == "grpc":
        return {"type": "grpc", "service_name": p.get("service_name") or ""}
    if network == "http":
        return {"type": "http", "host": [h.strip() for h in host.split(",") if h.strip()], "path": path}
    if network == "httpupgrade":
        return {"type": "httpupgrade", "host": host, "path": path}
    raise UnsupportedConfig(f"transport '{network}' is not supported by sing-box")


def _quic_tls(p: Dict[str, Any], host: str, alpn: list) -> Dict[str, Any]:
    """TLS block for the UDP/QUIC protocols (hysteria2, tuic)."""
    tls: Dict[str, Any] = {"enabled": True, "server_name": p.get("sni") or host,
                           "alpn": list(p.get("alpn") or alpn)}
    # A pinned self-signed certificate cannot be verified against system CAs; for a
    # reachability test we accept it rather than reporting a false "dead".
    if p.get("allow_insecure") or p.get("pin"):
        tls["insecure"] = True
    return tls


def build_outbound(cfg: ProxyConfig, tag: str = "proxy") -> Dict[str, Any]:
    """Translate a ProxyConfig into a sing-box outbound. Raises UnsupportedConfig."""
    p = cfg.params
    out: Dict[str, Any] = {"tag": tag, "server": cfg.host, "server_port": cfg.port}

    if cfg.protocol == "vmess":
        out.update(type="vmess", uuid=p["uuid"], security=p.get("cipher", "auto"),
                   alter_id=int(p.get("alter_id", 0)))
    elif cfg.protocol == "vless":
        if p.get("encryption", "none") != "none":
            raise UnsupportedConfig("VLESS encryption is not supported by sing-box")
        out.update(type="vless", uuid=p["uuid"])
        if p.get("flow"):
            out["flow"] = p["flow"]
    elif cfg.protocol == "trojan":
        out.update(type="trojan", password=p["password"])
    elif cfg.protocol == "ss":
        if p.get("plugin", "") not in _SS_PLUGINS:
            raise UnsupportedConfig(f"shadowsocks plugin '{p['plugin']}' is not supported")
        out.update(type="shadowsocks", method=p["method"], password=p["password"])
        if p.get("plugin"):
            out["plugin"] = p["plugin"]
            out["plugin_opts"] = p.get("plugin_opts", "")
        return out
    elif cfg.protocol == "hysteria2":
        out.update(type="hysteria2", password=p["password"], tls=_quic_tls(p, cfg.host, ["h3"]))
        if p.get("obfs"):
            out["obfs"] = {"type": p["obfs"], "password": p["obfs_password"]}
        return out
    elif cfg.protocol == "tuic":
        out.update(type="tuic", uuid=p["uuid"], password=p["password"],
                   congestion_control=p.get("congestion_control", "bbr"),
                   udp_relay_mode=p.get("udp_relay_mode", "native"),
                   tls=_quic_tls(p, cfg.host, ["h3"]))
        return out
    else:
        raise UnsupportedConfig(f"protocol '{cfg.protocol}' not supported")

    # vmess / vless / trojan share TLS + transport handling
    tls = _tls(p, cfg.host)
    if tls:
        out["tls"] = tls
    transport = _transport(p)
    if transport:
        out["transport"] = transport
    return out


def build_config(outbound: Dict[str, Any], listen_port: int) -> Dict[str, Any]:
    """Minimal sing-box config: one local "mixed" (HTTP + SOCKS5) inbound -> the outbound under test."""
    return {
        "log": {"level": "error", "timestamp": False},
        "inbounds": [{"type": "mixed", "tag": "in", "listen": "127.0.0.1", "listen_port": listen_port}],
        "outbounds": [outbound, {"type": "direct", "tag": "direct"}],
        "route": {"final": outbound["tag"]},
    }


# --------------------------------------------------------------------------- #
# Process management                                                          #
# --------------------------------------------------------------------------- #

_LIVE: Set[subprocess.Popen] = set()
_LIVE_LOCK = threading.Lock()
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def kill_all() -> None:
    """Kill every core process we started (atexit hook + Ctrl-C handler)."""
    with _LIVE_LOCK:
        procs = list(_LIVE)
        _LIVE.clear()
    for proc in procs:
        try:
            proc.kill()
        except OSError:
            pass


atexit.register(kill_all)


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class CoreProcess:
    """Context manager: start sing-box for one outbound, expose `.proxies`, always clean up.

        with CoreProcess(binary, outbound) as core:
            requests.get(url, proxies=core.proxies)
    """

    def __init__(self, binary: Path, outbound: Dict[str, Any], startup_timeout: float = 6.0):
        self.binary = Path(binary)
        self.outbound = outbound
        self.startup_timeout = float(startup_timeout)
        self.port = 0
        self._proc: Optional[subprocess.Popen] = None
        self._log = None
        self._config_path: Optional[str] = None

    @property
    def proxies(self) -> Dict[str, str]:
        url = f"http://127.0.0.1:{self.port}"
        return {"http": url, "https": url}

    def __enter__(self) -> "CoreProcess":
        for attempt in range(3):  # a random port can (rarely) be grabbed by someone else
            try:
                self._start()
                return self
            except CoreError as exc:
                self._cleanup()
                if "address already in use" not in str(exc).lower() or attempt == 2:
                    raise
        raise CoreError("could not start core")  # pragma: no cover

    def __exit__(self, *_exc: object) -> None:
        self._cleanup()

    def _start(self) -> None:
        self.port = free_port()
        fd, self._config_path = tempfile.mkstemp(prefix="v2s_", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(build_config(self.outbound, self.port), handle)
        self._log = tempfile.TemporaryFile()
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        try:
            self._proc = subprocess.Popen(
                [str(self.binary), "run", "-c", self._config_path],
                stdout=subprocess.DEVNULL, stderr=self._log, stdin=subprocess.DEVNULL,
                creationflags=flags,
            )
        except OSError as exc:
            raise CoreError(f"cannot execute {self.binary}: {exc}") from exc
        with _LIVE_LOCK:
            _LIVE.add(self._proc)

        deadline = time.monotonic() + self.startup_timeout
        while time.monotonic() < deadline:
            if self._proc.poll() is not None:
                raise CoreError(self._read_log() or f"core exited with code {self._proc.returncode}")
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.25):
                    return
            except OSError:
                time.sleep(0.05)
        raise CoreError("core did not open its port in time")

    def _read_log(self) -> str:
        try:
            self._log.seek(0)
            text = self._log.read().decode("utf-8", "replace")
        except (OSError, ValueError, AttributeError):
            return ""
        return _ANSI_RE.sub("", text).strip().replace("\n", " | ")[-300:]

    def _cleanup(self) -> None:
        proc, self._proc = self._proc, None
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    pass
            except OSError:
                pass
            with _LIVE_LOCK:
                _LIVE.discard(proc)
        if self._log is not None:
            try:
                self._log.close()
            except OSError:
                pass
            self._log = None
        if self._config_path:
            try:
                os.unlink(self._config_path)
            except OSError:
                pass
            self._config_path = None
