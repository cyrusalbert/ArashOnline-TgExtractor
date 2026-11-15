"""The shared pipeline used by BOTH workflows.

    GitHub Actions :  python -m src.pipeline --profile ci
    Local PC       :  python run_local.py            (thin wrapper, profile "local")

Stages:  scrape -> extract+validate -> raw_links.txt -> dedupe -> test -> rename -> output
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from itertools import zip_longest
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import output
from .config import load_config, load_dotenv
from .core_installer import InstallError, ensure_core
from .logger import setup_logging
from .models import ProxyConfig, TestResult
from .parser import dedupe, extract_candidates, parse_candidates
from .scraper import ScraperError, load_channels, scrape
from .tester import Tester

log = logging.getLogger("v2s.pipeline")

ROOT = Path(__file__).resolve().parent.parent


def _round_robin(configs: List[ProxyConfig]) -> List[ProxyConfig]:
    """Interleave configs channel by channel (newest first within a channel) so that a
    `max_configs` cap samples every channel fairly instead of only the first ones."""
    by_channel: Dict[str, List[ProxyConfig]] = {}
    for c in configs:
        by_channel.setdefault(c.source, []).append(c)
    mixed = [c for group in zip_longest(*by_channel.values()) for c in group if c is not None]
    return mixed


def run(cfg: Dict[str, Any], root: Path, *, scrape_only: bool = False, push: bool = False,
        update_core: bool = False) -> int:
    """Run the whole pipeline once. Returns a process exit code (0 = success)."""
    started = time.monotonic()
    out_dir = (root / str(cfg["output"]["dir"])).resolve()
    stats: Dict[str, Any] = {"profile": cfg.get("_profile", "")}

    # 1. Scrape ------------------------------------------------------------- #
    channels = load_channels(root / str(cfg["channels_file"]))
    if not channels:
        log.error("No channels found in %s - add some (one per line) and re-run",
                  cfg["channels_file"])
        return 1
    log.info("Scraping %d channel(s) in %r mode", len(channels), cfg["scraper"]["mode"])
    messages = scrape(cfg, channels)
    stats.update(channels=len(channels), messages=len(messages))
    if not messages:
        log.error("No messages could be fetched (network blocked? channel without web preview?)")
        return 1

    # 2. Extract + validate --------------------------------------------------- #
    max_len = int(cfg["parser"]["max_link_length"])
    candidates = [(m.channel, link) for m in messages for link in extract_candidates(m.text, max_len)]
    valid, rejected = parse_candidates(candidates)
    stats.update(candidates=len(candidates), valid=len(valid), rejected=dict(rejected))
    log.info("%d candidates -> %d valid links; rejected: %s",
             len(candidates), len(valid), dict(rejected) or "none")
    raw_path = output.write_raw_links(out_dir, valid)

    # 3. Dedupe ---------------------------------------------------------------- #
    unique = dedupe(valid)
    stats["unique"] = len(unique)
    log.info("%d unique configs after de-duplication", len(unique))
    if scrape_only:
        log.info("--scrape-only: stopping before the test stage")
        return _finish(cfg, root, push, [raw_path])

    # 4. Test ---------------------------------------------------------------- #
    selected = _round_robin(unique)[: int(cfg["testing"]["max_configs"])]
    if len(selected) < len(unique):
        log.info("Testing %d of %d (testing.max_configs cap)", len(selected), len(unique))
    stats["tested"] = len(selected)
    if not selected:
        log.error("Nothing to test - no valid links were found")
        return 1

    try:
        binary = ensure_core(cfg, root, force=update_core)
    except InstallError as exc:
        log.error("Cannot get the proxy core: %s", exc)
        return 1
    results: List[TestResult] = Tester(cfg, binary).run(selected)

    outcome = Counter(r.status for r in results)
    stats["outcome"] = dict(outcome)
    stats["alive"] = sum(1 for r in results if r.alive)
    stats["speed_tested"] = sum(1 for r in results if r.alive and r.speed_mbps is not None)
    errors = Counter(r.error.split(":")[0][:60] for r in results if r.status in ("core-error", "error"))
    if errors:
        log.info("Most common core/test errors: %s", errors.most_common(3))
    log.info("Test outcome: %s", dict(outcome))

    # 5 + 6. Rename + output ---------------------------------------------------- #
    if stats["alive"] == 0:
        if cfg["output"].get("keep_old_if_empty", True):
            log.error("No working config found - previous output files are left untouched")
        else:
            output.write_outputs(out_dir, results, cfg, stats)
        return 1

    stats["duration_seconds"] = round(time.monotonic() - started)
    written = [raw_path] + output.write_outputs(out_dir, results, cfg, stats)
    log.info("Done in %ds: %d alive, %d with speed", stats["duration_seconds"],
             stats["alive"], stats["speed_tested"])
    return _finish(cfg, root, push, written)


def _finish(cfg: Dict[str, Any], root: Path, push: bool, written: List[Path]) -> int:
    if not push:
        return 0
    message = str(cfg["output"]["commit_message"]).replace(
        "{utc}", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))
    return 0 if output.git_commit_push(root, written, message) else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Collect, test and publish V2Ray configs from Telegram.")
    p.add_argument("--config", default="config.yaml", help="config file (default: config.yaml)")
    p.add_argument("--profile", default=None, help="config profile: ci | local (env: V2S_PROFILE)")
    p.add_argument("--push", action="store_true", help="git commit + push the output files afterwards")
    p.add_argument("--scrape-only", action="store_true", help="stop after raw_links.txt (no testing)")
    p.add_argument("--max-configs", type=int, help="override testing.max_configs")
    p.add_argument("--messages", type=int, help="override scraper.messages_per_channel")
    p.add_argument("--no-speed", action="store_true", help="skip the speed test")
    p.add_argument("--no-geo", action="store_true", help="skip the geo-location test")
    p.add_argument("--update-core", action="store_true", help="re-download the sing-box binary")
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    return p


def main(argv: Optional[List[str]] = None, default_profile: Optional[str] = None) -> int:
    args = build_parser().parse_args(argv)
    os.chdir(ROOT)
    load_dotenv(ROOT / ".env")
    setup_logging("DEBUG" if args.verbose else "INFO")

    cfg_path = Path(args.config)
    if not cfg_path.is_absolute():
        cfg_path = ROOT / cfg_path
    profile = args.profile or os.environ.get("V2S_PROFILE") or default_profile
    cfg = load_config(cfg_path, profile)

    # CLI flags beat the config file
    if args.max_configs:
        cfg["testing"]["max_configs"] = args.max_configs
    if args.messages:
        cfg["scraper"]["messages_per_channel"] = args.messages
    if args.no_speed:
        cfg["testing"]["speed"]["enabled"] = False
    if args.no_geo:
        cfg["testing"]["geo"]["enabled"] = False
    setup_logging("DEBUG" if args.verbose else cfg["logging"]["level"], cfg["logging"]["file"])

    log.info("Profile: %s", profile or "(none)")
    try:
        return run(cfg, ROOT, scrape_only=args.scrape_only, push=args.push,
                   update_core=args.update_core)
    except KeyboardInterrupt:
        log.warning("Interrupted by user")
        return 130
    except ScraperError as exc:
        log.error("%s", exc)
        return 1
    except Exception:  # nothing may escape as a raw traceback-less crash
        log.exception("Unexpected fatal error")
        return 1


if __name__ == "__main__":
    sys.exit(main())
