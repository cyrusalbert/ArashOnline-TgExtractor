"""Link extraction, strict parsing/validation and de-duplication.

Pipeline position:   scraper -> **parser** -> tester -> renamer -> output

Public API
    extract_candidates(text)      -> list[str]   raw "scheme://..." strings found in free text
    parse_link(link)              -> ProxyConfig (raises InvalidLink with a short reason)
    parse_candidates(pairs)       -> (list[ProxyConfig], Counter of rejection reasons)
    dedupe(configs)               -> list[ProxyConfig] (remark-independent)
    b64decode_text(data)          -> str          tolerant base64 (std + url-safe, any padding)

Every parser returns *normalised* params so the de-duplicator and the sing-box
config builder never have to care which URI dialect a link was written in.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import html
import ipaddress
import json
import logging
import re
import uuid
from collections import Counter
from typing import Any, Dict, Iterable, List, Tuple
from urllib.parse import parse_qsl, unquote, urlsplit

from .models import ProxyConfig

log = logging.getLogger("v2s.parser")


class InvalidLink(ValueError):
    """A candidate link is malformed. The message is a short, countable reason."""


# --------------------------------------------------------------------------- #
# 1. Extraction from free text                                                #
# --------------------------------------------------------------------------- #

# "ss" gets a look-behind so words like "access://" are not mistaken for a link.
# Other schemes deliberately have none, so links glued together without
# whitespace ("...#namevmess://...") are still split correctly.
_SCHEME_RE = re.compile(r"(?i)(?:vmess|vless|trojan|hysteria2|hy2|tuic|(?<![a-z0-9])ss)://")
_STOP_RE = re.compile(r"[\s<>\"'`\\]")
_ENTITY_RE = re.compile(r"&(?:#\d+|#[xX][0-9a-fA-F]+|[A-Za-z][A-Za-z0-9]*);")
_ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff\u00ad"))


def normalize_text(text: str) -> str:
    """Undo HTML escaping (also double-escaped) and strip invisible characters.

    Only *terminated* entities are decoded: html.unescape() on its own would turn
    "&copy=1" in a query string into "(c)=1" and corrupt the link.
    """
    for _ in range(3):
        new = _ENTITY_RE.sub(lambda m: html.unescape(m.group(0)), text)
        if new == text:
            break
        text = new
    text = text.replace("\\u0026", "&").replace("\\/", "/")
    return text.translate(_ZERO_WIDTH)


def extract_candidates(text: str, max_len: int = 4096) -> List[str]:
    """Return every substring that *looks like* a supported link (not yet validated)."""
    text = normalize_text(text)
    starts = [m.start() for m in _SCHEME_RE.finditer(text)]
    found: List[str] = []
    seen = set()
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(text)
        chunk = text[start:end]
        stop = _STOP_RE.search(chunk)
        if stop:
            chunk = chunk[: stop.start()]
        chunk = chunk.rstrip(".,;:!?)]}*")  # punctuation / markdown that is not part of the link
        if 10 <= len(chunk) <= max_len and chunk not in seen:
            seen.add(chunk)
            found.append(chunk)
    return found


# --------------------------------------------------------------------------- #
# 2. Small validation helpers                                                 #
# --------------------------------------------------------------------------- #

_HOST_RE = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?\.)+[a-z0-9_-]{2,63}$", re.I
)

_VMESS_CIPHERS = {"auto", "aes-128-gcm", "chacha20-poly1305", "none", "zero"}
_NETWORKS = {"tcp", "ws", "grpc", "http", "httpupgrade", "kcp", "quic", "xhttp"}
_NET_ALIASES = {"h2": "http", "websocket": "ws", "splithttp": "xhttp", "mkcp": "kcp",
                "raw": "tcp", "gun": "grpc"}
_SS_METHODS = {
    "2022-blake3-aes-128-gcm", "2022-blake3-aes-256-gcm", "2022-blake3-chacha20-poly1305",
    "none", "aes-128-gcm", "aes-192-gcm", "aes-256-gcm", "chacha20-ietf-poly1305",
    "xchacha20-ietf-poly1305", "aes-128-ctr", "aes-192-ctr", "aes-256-ctr",
    "aes-128-cfb", "aes-192-cfb", "aes-256-cfb", "rc4-md5", "chacha20-ietf", "xchacha20",
}


def b64decode_text(data: str) -> str:
    """Decode standard *or* url-safe base64 with missing/extra padding. Raises InvalidLink."""
    cleaned = re.sub(r"\s+", "", data).rstrip("=")
    cleaned += "=" * (-len(cleaned) % 4)
    try:
        return base64.b64decode(cleaned, altchars=b"-_", validate=True).decode("utf-8")
    except (binascii.Error, ValueError) as exc:  # UnicodeDecodeError is a ValueError
        raise InvalidLink("invalid base64") from exc


def _validate_host(host: str) -> str:
    """Accept public IPs and well-formed domain names only."""
    host = (host or "").strip().strip("[]").rstrip(".").lower()
    if not host:
        raise InvalidLink("missing host")
    if not host.isascii():
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise InvalidLink("invalid host") from exc
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        if host == "localhost" or re.fullmatch(r"[\d.]+", host) or not _HOST_RE.match(host):
            raise InvalidLink("invalid host") from None
        return host
    if not ip.is_global:  # loopback / private / link-local / unspecified: useless and unsafe to dial
        raise InvalidLink("non-public IP")
    return host


def _port(value: Any) -> int:
    try:
        port = int(str(value).strip())
    except (TypeError, ValueError):
        raise InvalidLink("invalid port") from None
    if not 1 <= port <= 65535:
        raise InvalidLink("port out of range")
    return port


def _uuid(value: Any) -> str:
    try:
        return str(uuid.UUID(str(value).strip()))
    except (ValueError, AttributeError):
        raise InvalidLink("invalid uuid") from None


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _network(value: Any) -> str:
    net = str(value or "tcp").strip().lower()
    net = _NET_ALIASES.get(net, net)
    if net not in _NETWORKS:
        raise InvalidLink("unknown transport")
    return net


def _split_hostport(hostport: str) -> Tuple[str, str]:
    """'host:port' / '[v6]:port' -> (host, port_text). Port text may be empty."""
    hostport = hostport.strip()
    if hostport.startswith("["):
        end = hostport.find("]")
        if end == -1:
            raise InvalidLink("unterminated IPv6 literal")
        host, rest = hostport[1:end], hostport[end + 1:]
        if rest and not rest.startswith(":"):
            raise InvalidLink("garbage after IPv6 literal")
        return host, rest[1:]
    if hostport.count(":") > 1:
        raise InvalidLink("ambiguous host:port")
    host, _, port = hostport.partition(":")
    return host, port


def _split_netloc(netloc: str) -> Tuple[str, str, str]:
    """'user:pw@host:port' -> (raw userinfo, host, port_text). Userinfo is NOT unquoted yet."""
    userinfo, _, hostport = netloc.rpartition("@")
    host, port = _split_hostport(hostport)
    return userinfo, host, port


def _url_parts(link: str) -> Tuple[str, str, str, Dict[str, str], str]:
    parts = urlsplit(link)
    userinfo, host, port_text = _split_netloc(parts.netloc)
    query = {k.lower(): v for k, v in parse_qsl(parts.query, keep_blank_values=True)}
    return userinfo, host, port_text, query, unquote(parts.fragment)


def _list(value: str) -> List[str]:
    return [v.strip() for v in str(value or "").split(",") if v.strip()]


def _transport_params(q: Dict[str, str], default_security: str) -> Dict[str, Any]:
    """Normalise the query-string dialect shared by vless:// and trojan://."""
    security = (q.get("security") or default_security).lower()
    if security == "xtls":
        security = "tls"
    if security not in ("none", "tls", "reality"):
        raise InvalidLink("unknown security")
    return {
        "network": _network(q.get("type") or q.get("network")),
        "security": security,
        "sni": q.get("sni") or q.get("peer") or q.get("servername") or "",
        "host": q.get("host", ""),
        "path": q.get("path", ""),
        "service_name": q.get("servicename") or q.get("service_name") or "",
        "alpn": _list(q.get("alpn", "")),
        "fp": q.get("fp", ""),
        "allow_insecure": _truthy(q.get("allowinsecure") or q.get("insecure")),
        "pbk": q.get("pbk", ""),
        "sid": q.get("sid", ""),
        "header_type": (q.get("headertype") or "").lower(),
    }


# --------------------------------------------------------------------------- #
# 3. Per-protocol parsers  ->  (host, port, remark, params)                   #
# --------------------------------------------------------------------------- #

Parsed = Tuple[str, int, str, Dict[str, Any]]


def _parse_vmess(link: str) -> Parsed:
    body = re.split(r"[#?]", link[len("vmess://"):], maxsplit=1)[0]
    if not body:
        raise InvalidLink("empty vmess body")
    try:
        data = json.loads(b64decode_text(body))
    except ValueError:  # covers InvalidLink and JSONDecodeError
        raise InvalidLink("vmess payload is not base64 JSON") from None
    if not isinstance(data, dict):
        raise InvalidLink("vmess JSON is not an object")

    host = str(data.get("add") or "")
    port = _port(data.get("port"))
    uid = _uuid(data.get("id"))
    try:
        alter_id = int(data.get("aid") or 0)
    except (TypeError, ValueError):
        raise InvalidLink("invalid alterId") from None
    if not 0 <= alter_id <= 65535:
        raise InvalidLink("invalid alterId")
    cipher = str(data.get("scy") or "auto").lower()
    if cipher not in _VMESS_CIPHERS:
        raise InvalidLink("unknown vmess cipher")

    network = _network(data.get("net"))
    path = str(data.get("path") or "")
    params = {
        "uuid": uid,
        "alter_id": alter_id,
        "cipher": cipher,
        "network": network,
        "security": "tls" if str(data.get("tls") or "").lower() in ("tls", "xtls") else "none",
        "sni": str(data.get("sni") or ""),
        "host": str(data.get("host") or ""),
        "path": path,
        "service_name": path if network == "grpc" else "",  # vmess stores gRPC service name in "path"
        "alpn": _list(data.get("alpn", "")),
        "fp": str(data.get("fp") or ""),
        "allow_insecure": _truthy(data.get("allowInsecure") or data.get("insecure")),
        "header_type": str(data.get("type") or "").lower(),
    }
    return host, port, str(data.get("ps") or ""), params


def _parse_vless(link: str) -> Parsed:
    userinfo, host, port_text, q, remark = _url_parts(link)
    uid = _uuid(unquote(userinfo))
    port = _port(port_text)
    params = _transport_params(q, default_security="none")
    params["uuid"] = uid
    params["flow"] = q.get("flow", "")
    params["encryption"] = (q.get("encryption") or "none").lower()
    if params["security"] == "reality":
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}", params["pbk"]):
            raise InvalidLink("invalid reality public key")
        if not re.fullmatch(r"[0-9a-fA-F]{0,16}", params["sid"]):
            raise InvalidLink("invalid reality short id")
    return host, port, remark, params


def _parse_trojan(link: str) -> Parsed:
    userinfo, host, port_text, q, remark = _url_parts(link)
    password = unquote(userinfo)
    if not password:
        raise InvalidLink("missing password")
    params = _transport_params(q, default_security="tls")
    params["password"] = password
    return host, _port(port_text), remark, params


def _parse_ss(link: str) -> Parsed:
    """Handles SIP002 (ss://b64(method:pw)@host:port, also plain method:pw@) and legacy
    (ss://b64(method:pw@host:port)) forms, plus ?plugin=..."""
    body, _, fragment = link[len("ss://"):].partition("#")
    main, _, query = body.partition("?")
    main = main.rstrip("/")
    if not main:
        raise InvalidLink("empty ss body")

    if "@" in main:
        userinfo, _, hostport = main.rpartition("@")
        userinfo = unquote(userinfo)
        if ":" not in userinfo:                      # base64-wrapped "method:password"
            userinfo = b64decode_text(userinfo)
    else:                                            # legacy: everything is base64
        decoded = b64decode_text(unquote(main))
        if "@" not in decoded:
            raise InvalidLink("ss payload has no server")
        userinfo, _, hostport = decoded.rpartition("@")

    method, _, password = userinfo.partition(":")
    method = method.strip().lower()
    if method not in _SS_METHODS:
        raise InvalidLink("unsupported ss cipher")
    if not password and method != "none":
        raise InvalidLink("missing password")

    host, port_text = _split_hostport(hostport)
    q = {k.lower(): v for k, v in parse_qsl(query, keep_blank_values=True)}
    plugin_name, _, plugin_opts = (q.get("plugin") or "").partition(";")
    if plugin_name in ("simple-obfs", "obfs"):
        plugin_name = "obfs-local"
    params = {"method": method, "password": password, "plugin": plugin_name, "plugin_opts": plugin_opts}
    return host, _port(port_text), unquote(fragment), params


def _parse_hysteria2(link: str) -> Parsed:
    userinfo, host, port_text, q, remark = _url_parts(link)
    auth = unquote(userinfo)
    if not auth:
        raise InvalidLink("missing auth")
    # Port may be omitted (443) or a list/range such as "443,5000-6000": use the first number.
    first_port = re.split(r"[,\-]", port_text)[0] if port_text else "443"
    obfs = (q.get("obfs") or "").lower()
    obfs_password = q.get("obfs-password") or q.get("obfs_password") or ""
    if obfs and obfs != "salamander":
        raise InvalidLink("unknown hysteria2 obfs")
    if obfs and not obfs_password:
        raise InvalidLink("obfs without password")
    params = {
        "password": auth,
        "sni": q.get("sni") or q.get("peer") or "",
        "allow_insecure": _truthy(q.get("insecure") or q.get("allowinsecure")),
        "obfs": obfs,
        "obfs_password": obfs_password,
        "pin": q.get("pinsha256", ""),
        "alpn": _list(q.get("alpn", "")),
    }
    return host, _port(first_port), remark, params


def _parse_tuic(link: str) -> Parsed:
    userinfo, host, port_text, q, remark = _url_parts(link)
    raw_uuid, _, raw_password = userinfo.partition(":")
    password = unquote(raw_password)
    if not password:
        raise InvalidLink("missing password")
    params = {
        "uuid": _uuid(unquote(raw_uuid)),
        "password": password,
        "sni": q.get("sni", ""),
        "allow_insecure": _truthy(q.get("allow_insecure") or q.get("allowinsecure") or q.get("insecure")),
        "alpn": _list(q.get("alpn", "")),
        "congestion_control": (q.get("congestion_control") or q.get("congestion-control") or "bbr").lower(),
        "udp_relay_mode": (q.get("udp_relay_mode") or q.get("udp-relay-mode") or "native").lower(),
    }
    return host, _port(port_text), remark, params


_PARSERS = {
    "vmess": _parse_vmess,
    "vless": _parse_vless,
    "trojan": _parse_trojan,
    "ss": _parse_ss,
    "hysteria2": _parse_hysteria2,
    "tuic": _parse_tuic,
}
_SCHEME_ALIASES = {"hy2": "hysteria2"}


# --------------------------------------------------------------------------- #
# 4. Public parsing + de-duplication                                          #
# --------------------------------------------------------------------------- #

# Fields that define "the same server config". Remarks and cosmetic params
# (fingerprint, ALPN order...) are intentionally NOT part of the key.
_KEY_FIELDS = ("uuid", "password", "method", "network", "security", "sni", "host", "path",
               "service_name", "flow", "pbk", "sid", "obfs", "obfs_password", "plugin", "header_type")


def _dedupe_key(cfg: ProxyConfig) -> str:
    p = cfg.params
    parts = [cfg.protocol, cfg.host, str(cfg.port)]
    parts += [f"{name}={p[name]}" for name in _KEY_FIELDS if p.get(name)]
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()


def parse_link(link: str, source: str = "") -> ProxyConfig:
    """Parse and strictly validate one link. Raises InvalidLink (never anything else)."""
    scheme, sep, _ = link.partition("://")
    if not sep:
        raise InvalidLink("no scheme")
    protocol = _SCHEME_ALIASES.get(scheme.lower(), scheme.lower())
    parser = _PARSERS.get(protocol)
    if parser is None:
        raise InvalidLink("unsupported scheme")
    try:
        host, port, remark, params = parser(link)
        host = _validate_host(host)
    except InvalidLink:
        raise
    except Exception as exc:  # urlsplit() ValueError & friends: still just "malformed"
        raise InvalidLink(f"unparseable ({type(exc).__name__})") from exc
    cfg = ProxyConfig(protocol=protocol, host=host, port=port, params=params,
                      link=link, remark=remark, source=source)
    cfg.key = _dedupe_key(cfg)
    return cfg


def parse_candidates(candidates: Iterable[Tuple[str, str]]) -> Tuple[List[ProxyConfig], Counter]:
    """Validate (source, link) pairs. One bad link can never stop the others.

    Exact textual duplicates are dropped here; semantic de-dup is `dedupe()`.
    """
    valid: List[ProxyConfig] = []
    rejected: Counter = Counter()
    seen = set()
    for source, link in candidates:
        if link in seen:
            continue
        seen.add(link)
        try:
            valid.append(parse_link(link, source))
        except InvalidLink as exc:
            rejected[str(exc)] += 1
        except Exception as exc:  # defensive: a parser bug must not kill the run
            log.debug("Unexpected parser error for %.60s: %r", link, exc)
            rejected["internal parser error"] += 1
    return valid, rejected


def dedupe(configs: Iterable[ProxyConfig]) -> List[ProxyConfig]:
    """Keep the first occurrence of every protocol+host+port+credentials+transport combo."""
    unique: Dict[str, ProxyConfig] = {}
    for cfg in configs:
        unique.setdefault(cfg.key, cfg)
    return list(unique.values())
