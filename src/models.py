"""Plain data containers shared by every module."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class ProxyConfig:
    """One validated proxy link in a normalised, protocol-independent form."""

    protocol: str                 # vmess | vless | trojan | ss | hysteria2 | tuic
    host: str                     # lower-cased server host / IP
    port: int
    params: Dict[str, Any]        # normalised credentials + transport/TLS settings
    link: str                     # the link exactly as found (remark untouched)
    remark: str = ""              # original remark (only used for logging)
    source: str = ""              # channel the link came from
    key: str = ""                 # de-duplication key (remark-independent)


@dataclass
class TestResult:
    """Outcome of testing one ProxyConfig."""

    __test__ = False  # stop pytest from trying to collect this as a test class

    config: ProxyConfig
    status: str = "pending"       # ok | dead | unsupported | core-error | skipped | filtered | error
    alive: bool = False
    ping_ms: Optional[int] = None
    speed_mbps: Optional[float] = None
    country_code: str = "XX"
    country: str = ""
    exit_ip: str = ""
    error: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)
