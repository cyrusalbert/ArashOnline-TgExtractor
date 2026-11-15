"""Telegram scraping.

Two back-ends, selected by `scraper.mode` in config.yaml:

  web       Public preview pages (https://t.me/s/<channel>). No login, no API keys.
            Works only for channels that have the web preview enabled.
  telethon  The real Telegram API through a user session (optional dependency).
            Credentials come from environment variables / GitHub secrets only:
              TELEGRAM_API_ID, TELEGRAM_API_HASH, TELEGRAM_SESSION (a StringSession)
  both      web first, then Telethon for extra coverage.

Both back-ends return ScrapedMessage objects whose `text` is simply handed to
parser.extract_candidates(); the scraper itself never judges what a link is.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
from bs4 import BeautifulSoup

log = logging.getLogger("v2s.scraper")


class ScraperError(RuntimeError):
    """Configuration-level scraping problem (never raised for a single bad channel)."""


@dataclass(frozen=True)
class ScrapedMessage:
    channel: str
    msg_id: int
    text: str  # visible text + raw inner HTML + href values (all fed to the link extractor)


# --------------------------------------------------------------------------- #
# channels.txt                                                                #
# --------------------------------------------------------------------------- #

_CHANNEL_RE = re.compile(r"^[A-Za-z0-9_]{3,64}$")
_URL_PREFIX_RE = re.compile(r"^(?:https?://)?(?:www\.)?(?:t|telegram)\.me/(?:s/)?", re.I)


def normalize_channel(line: str) -> Optional[str]:
    """'@name', 'https://t.me/name', 't.me/s/name', 'name  # comment' -> 'name' (or None)."""
    line = line.split("#", 1)[0].strip()
    if not line:
        return None
    line = _URL_PREFIX_RE.sub("", line).lstrip("@")
    name = re.split(r"[/?\s]", line, maxsplit=1)[0]
    return name if _CHANNEL_RE.match(name) else None


def load_channels(path: Path) -> List[str]:
    """Read channels.txt (one per line, # comments allowed). Duplicates removed, order kept."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ScraperError(f"Cannot read channel list {path}: {exc}") from exc
    channels: List[str] = []
    seen = set()
    for number, line in enumerate(lines, 1):
        stripped = line.split("#", 1)[0].strip()
        if not stripped:
            continue
        name = normalize_channel(line)
        if name is None:
            log.warning("%s:%d - cannot understand %r, skipping", path.name, number, stripped)
        elif name.lower() not in seen:
            seen.add(name.lower())
            channels.append(name)
    return channels


# --------------------------------------------------------------------------- #
# Web preview back-end                                                        #
# --------------------------------------------------------------------------- #

def parse_preview_page(page_html: str, channel: str) -> List[ScrapedMessage]:
    """Extract messages from one t.me/s/<channel> HTML page."""
    soup = BeautifulSoup(page_html, "html.parser")
    messages: List[ScrapedMessage] = []
    for node in soup.select("div.tgme_widget_message[data-post]"):
        try:
            msg_id = int(str(node.get("data-post", "")).rsplit("/", 1)[1])
        except (IndexError, ValueError):
            continue
        parts: List[str] = []
        for block in node.select("div.tgme_widget_message_text"):
            parts.append(block.get_text("\n"))       # decoded visible text (entities resolved)
            parts.append(block.decode_contents())    # raw inner HTML (escaped / <code> / <pre> forms)
            parts.extend(str(a["href"]) for a in block.select("a[href]"))
        if parts:
            messages.append(ScrapedMessage(channel, msg_id, "\n".join(parts)))
    return messages


def _http_get(session: requests.Session, url: str, params: Optional[Dict[str, Any]],
              scfg: Dict[str, Any]) -> Optional[requests.Response]:
    """GET with retries and back-off. Returns None when every attempt failed."""
    retries = max(1, int(scfg["retries"]))
    for attempt in range(1, retries + 1):
        try:
            resp = session.get(url, params=params, timeout=float(scfg["request_timeout"]))
            if resp.status_code == 200:
                return resp
            if resp.status_code == 404:
                log.warning("%s -> HTTP 404 (no such channel?)", url)
                return None
            if resp.status_code == 429:
                wait = min(int(resp.headers.get("Retry-After", "5") or 5), 30)
                log.warning("Rate limited by Telegram, sleeping %ds", wait)
                time.sleep(wait)
                continue
            log.warning("%s -> HTTP %d (attempt %d/%d)", url, resp.status_code, attempt, retries)
        except (requests.RequestException, ValueError) as exc:
            log.warning("%s failed (attempt %d/%d): %s", url, attempt, retries, exc)
        time.sleep(1.5 * attempt)
    return None


def scrape_channel_web(session: requests.Session, channel: str, limit: int,
                       scfg: Dict[str, Any]) -> List[ScrapedMessage]:
    """Newest `limit` messages of one channel, following ?before=<id> pagination."""
    url = f"{str(scfg['base_url']).rstrip('/')}/{channel}"
    collected: Dict[int, ScrapedMessage] = {}
    before: Optional[int] = None
    for _page in range(max(1, int(scfg["max_pages_per_channel"]))):
        resp = _http_get(session, url, {"before": before} if before else None, scfg)
        if resp is None:
            break
        if "/s/" not in resp.url and "tgme_widget_message" not in resp.text:
            log.warning("@%s has no public web preview (try scraper.mode: telethon)", channel)
            break
        page = parse_preview_page(resp.text, channel)
        fresh = [m for m in page if m.msg_id not in collected]
        if not fresh:
            break
        collected.update({m.msg_id: m for m in fresh})
        before = min(collected)
        if len(collected) >= limit or before <= 1:
            break
        time.sleep(float(scfg["delay_between_requests"]))
    newest_first = sorted(collected.values(), key=lambda m: m.msg_id, reverse=True)
    return newest_first[:limit]


def scrape_web(channels: List[str], limit: int, scfg: Dict[str, Any]) -> List[ScrapedMessage]:
    session = requests.Session()
    session.headers.update({"User-Agent": scfg["user_agent"], "Accept-Language": "en"})
    if scfg.get("proxy"):
        session.proxies.update({"http": scfg["proxy"], "https": scfg["proxy"]})
    out: List[ScrapedMessage] = []
    for channel in channels:
        try:
            msgs = scrape_channel_web(session, channel, limit, scfg)
            log.info("@%s: %d messages", channel, len(msgs))
            out.extend(msgs)
        except Exception as exc:  # one broken channel must not stop the others
            log.warning("@%s: unexpected scraping error: %r", channel, exc)
        time.sleep(float(scfg["delay_between_requests"]))
    return out


# --------------------------------------------------------------------------- #
# Telethon back-end (optional)                                                #
# --------------------------------------------------------------------------- #

async def _telethon_async(channels: List[str], limit: int, api_id: int, api_hash: str,
                          session_string: str) -> List[ScrapedMessage]:
    try:
        from telethon import TelegramClient, errors
        from telethon.sessions import StringSession
    except ImportError as exc:
        raise ScraperError("Telethon mode needs:  pip install telethon") from exc

    out: List[ScrapedMessage] = []
    client = TelegramClient(StringSession(session_string), api_id, api_hash)
    await client.connect()
    try:
        # Never call client.start() here: with a bad session it would prompt for a phone
        # number and hang a CI job forever.
        if not await client.is_user_authorized():
            raise ScraperError("TELEGRAM_SESSION is invalid or expired - regenerate it "
                               "with scripts/telethon_session.py")
        for channel in channels:
            count = 0
            try:
                async for msg in client.iter_messages(channel, limit=limit):
                    parts = [msg.message or ""]
                    # URLs hidden behind formatted text ("[click](vless://...)")
                    parts += [getattr(e, "url", "") or "" for e in (msg.entities or [])]
                    text = "\n".join(p for p in parts if p)
                    if text:
                        out.append(ScrapedMessage(channel, int(msg.id), text))
                        count += 1
                log.info("@%s: %d messages (telethon)", channel, count)
            except errors.FloodWaitError as exc:
                log.warning("@%s: flood-wait %ss, skipping rest of channel", channel, exc.seconds)
            except Exception as exc:
                log.warning("@%s: telethon error: %r", channel, exc)
    finally:
        await client.disconnect()
    return out


def scrape_telethon(channels: List[str], limit: int) -> List[ScrapedMessage]:
    api_id = os.environ.get("TELEGRAM_API_ID", "").strip()
    api_hash = os.environ.get("TELEGRAM_API_HASH", "").strip()
    session = os.environ.get("TELEGRAM_SESSION", "").strip()
    if not (api_id.isdigit() and api_hash and session):
        raise ScraperError("Telethon mode needs TELEGRAM_API_ID, TELEGRAM_API_HASH and "
                           "TELEGRAM_SESSION in the environment (or .env / GitHub secrets)")
    return asyncio.run(_telethon_async(channels, limit, int(api_id), api_hash, session))


# --------------------------------------------------------------------------- #
# Entry point                                                                 #
# --------------------------------------------------------------------------- #

def scrape(cfg: Dict[str, Any], channels: List[str]) -> List[ScrapedMessage]:
    """Fetch messages from every channel using the configured back-end(s)."""
    scfg = cfg["scraper"]
    limit = max(1, int(scfg["messages_per_channel"]))
    mode = str(scfg["mode"]).lower()
    if mode not in ("web", "telethon", "both"):
        raise ScraperError(f"scraper.mode must be web, telethon or both (got {mode!r})")

    messages: List[ScrapedMessage] = []
    if mode in ("web", "both"):
        messages += scrape_web(channels, limit, scfg)
    if mode in ("telethon", "both"):
        try:
            messages += scrape_telethon(channels, limit)
        except ScraperError as exc:
            if mode == "telethon":
                raise
            log.warning("Telethon skipped: %s", exc)  # in "both" mode the web results still count
    return messages
