"""Exit-country lookup *through* the proxy under test.

The request leaves the tested config's server, so the answer is the country a
user of that config would appear in. Providers are tried in the order given by
`testing.geo.providers`; the first valid answer wins. Free providers rate-limit
per source IP - here that is the proxy's exit IP, not yours - so a failure just
falls through to the next provider.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Tuple

import requests

log = logging.getLogger("v2s.geo")


@dataclass
class GeoInfo:
    country_code: str
    country: str
    ip: str


def _ip_api(d: Dict[str, Any]) -> Optional[GeoInfo]:
    if d.get("status") != "success":
        return None
    return GeoInfo(d.get("countryCode", ""), d.get("country", ""), d.get("query", ""))


def _ipwho(d: Dict[str, Any]) -> Optional[GeoInfo]:
    if not d.get("success", False):
        return None
    return GeoInfo(d.get("country_code", ""), d.get("country", ""), d.get("ip", ""))


def _ipinfo(d: Dict[str, Any]) -> Optional[GeoInfo]:
    return GeoInfo(d.get("country", ""), "", d.get("ip", ""))


def _country_is(d: Dict[str, Any]) -> Optional[GeoInfo]:
    return GeoInfo(d.get("country", ""), "", d.get("ip", ""))


# name -> (URL, parser).  ip-api only offers plain HTTP on its free tier - acceptable
# here because only a country code is fetched.
PROVIDERS: Dict[str, Tuple[str, Callable[[Dict[str, Any]], Optional[GeoInfo]]]] = {
    "ip-api": ("http://ip-api.com/json/?fields=status,country,countryCode,query", _ip_api),
    "ipwho": ("https://ipwho.is/", _ipwho),
    "ipinfo": ("https://ipinfo.io/json", _ipinfo),
    "country-is": ("https://api.country.is/", _country_is),
}


def lookup(proxies: Dict[str, str], gcfg: Dict[str, Any]) -> Optional[GeoInfo]:
    """Return the exit country seen through `proxies`, or None if every provider failed."""
    timeout = float(gcfg.get("timeout", 8))
    for name in gcfg.get("providers", []):
        entry = PROVIDERS.get(str(name))
        if entry is None:
            log.debug("unknown geo provider %r", name)
            continue
        url, parse = entry
        try:
            with requests.Session() as session:
                session.trust_env = False
                session.proxies.update(proxies)
                resp = session.get(url, timeout=timeout, headers={"User-Agent": "curl/8.5.0"})
            if resp.status_code != 200:
                continue
            info = parse(resp.json())
        except (requests.RequestException, ValueError):
            continue
        if info and re.fullmatch(r"[A-Za-z]{2}", info.country_code or ""):
            info.country_code = info.country_code.upper()
            return info
    return None
