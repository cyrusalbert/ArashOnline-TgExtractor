"""Rewrite the remark (display name) of a link while keeping the link valid.

    vmess://     remark lives inside the base64 JSON ("ps")  -> decode, edit, re-encode
    everything   remark is the URL fragment (#...)           -> replace and percent-encode
    else

Nothing else in the link is touched, so credentials and transport settings are
preserved byte-for-byte.
"""
from __future__ import annotations

import base64
import json
import logging
import re
from typing import Any, Dict, List, Sequence
from urllib.parse import quote

from .models import TestResult
from .parser import InvalidLink, b64decode_text

log = logging.getLogger("v2s.renamer")

DEFAULT_TEMPLATE = "{flag} {cc} | {ping}ms | {speed}Mbps | #{rank:03d}"


def flag_emoji(country_code: str) -> str:
    """'DE' -> 🇩🇪 (regional-indicator letters). Unknown / invalid -> 🌐."""
    cc = (country_code or "").upper()
    if not re.fullmatch(r"[A-Z]{2}", cc) or cc == "XX":
        return "\U0001F310"
    return "".join(chr(0x1F1E6 + ord(c) - ord("A")) for c in cc)


def make_remark(result: TestResult, rank: int, template: str) -> str:
    """Fill the template. A broken user template falls back to the default."""
    fields: Dict[str, Any] = {
        "flag": flag_emoji(result.country_code),
        "cc": result.country_code or "XX",
        "country": result.country or result.country_code or "Unknown",
        "ping": result.ping_ms if result.ping_ms is not None else "?",
        "speed": f"{result.speed_mbps:.1f}" if result.speed_mbps is not None else "?",
        "rank": rank,
        "protocol": result.config.protocol,
    }
    try:
        remark = template.format(**fields)
    except (KeyError, IndexError, ValueError) as exc:
        log.warning("Bad rename.template (%s) - using the default", exc)
        remark = DEFAULT_TEMPLATE.format(**fields)
    return re.sub(r"[\r\n\t]+", " ", remark).strip()


def apply_remark(link: str, protocol: str, remark: str) -> str:
    """Return `link` with its remark replaced. On any problem the link is returned unchanged."""
    try:
        if protocol == "vmess":
            payload = re.split(r"[#?]", link[len("vmess://"):], maxsplit=1)[0]
            data = json.loads(b64decode_text(payload))
            data["ps"] = remark
            raw = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            return "vmess://" + base64.b64encode(raw).decode("ascii")
        return link.split("#", 1)[0] + "#" + quote(remark, safe="")
    except (InvalidLink, ValueError, TypeError) as exc:
        log.debug("Could not rename %.40s...: %s", link, exc)
        return link


def rename_links(results: Sequence[TestResult], template: str) -> List[str]:
    """Renamed links for an already-sorted list; rank = position in this list (1-based)."""
    return [apply_remark(r.config.link, r.config.protocol, make_remark(r, rank, template))
            for rank, r in enumerate(results, 1)]
