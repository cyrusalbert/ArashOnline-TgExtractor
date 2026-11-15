"""Ranking and writing of the published files.

    raw_links.txt          every valid link found (before de-dup / testing)
    sorted_by_ping.txt     alive configs, lowest ping first
    sorted_by_speed.txt    speed-tested configs, highest Mbps first
    sub_ping_base64.txt    base64 subscription version of the ping list
    sub_speed_base64.txt   base64 subscription version of the speed list
    results.json           full statistics

Files are written atomically (temp file + rename) so a crash never leaves a
half-written list behind.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .models import ProxyConfig, TestResult
from .renamer import rename_links

log = logging.getLogger("v2s.output")

RAW = "raw_links.txt"
PING = "sorted_by_ping.txt"
SPEED = "sorted_by_speed.txt"
SUB_PING = "sub_ping_base64.txt"
SUB_SPEED = "sub_speed_base64.txt"
JSON_FILE = "results.json"
ALL_FILES = (RAW, PING, SPEED, SUB_PING, SUB_SPEED, JSON_FILE)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    os.replace(tmp, path)


def _lines(links: Sequence[str]) -> str:
    return "\n".join(links) + "\n" if links else ""


def _b64(links: Sequence[str]) -> str:
    return base64.b64encode("\n".join(links).encode("utf-8")).decode("ascii") if links else ""


def write_raw_links(out_dir: Path, configs: Sequence[ProxyConfig]) -> Path:
    path = out_dir / RAW
    _write(path, _lines([c.link for c in configs]))
    return path


def rank_results(results: Sequence[TestResult], top_n: int = 0) -> Tuple[List[TestResult], List[TestResult]]:
    """(by_ping, by_speed). by_speed only contains configs that were actually speed-tested."""
    alive = [r for r in results if r.alive and r.ping_ms is not None]
    by_ping = sorted(alive, key=lambda r: (r.ping_ms, -(r.speed_mbps or 0.0)))
    by_speed = sorted((r for r in alive if r.speed_mbps is not None),
                      key=lambda r: (-(r.speed_mbps or 0.0), r.ping_ms))
    if top_n > 0:
        by_ping, by_speed = by_ping[:top_n], by_speed[:top_n]
    return by_ping, by_speed


def write_outputs(out_dir: Path, results: Sequence[TestResult], cfg: Dict[str, Any],
                  stats: Dict[str, Any]) -> List[Path]:
    """Write the sorted lists (+ optional base64 subs and results.json). Returns written paths."""
    ocfg = cfg["output"]
    template = str(cfg["rename"]["template"])
    by_ping, by_speed = rank_results(results, int(ocfg.get("top_n", 0) or 0))
    ping_links = rename_links(by_ping, template)
    speed_links = rename_links(by_speed, template)

    written = []
    for name, text in ((PING, _lines(ping_links)), (SPEED, _lines(speed_links))):
        _write(out_dir / name, text)
        written.append(out_dir / name)

    if ocfg.get("base64_subscriptions", True):
        for name, links in ((SUB_PING, ping_links), (SUB_SPEED, speed_links)):
            _write(out_dir / name, _b64(links))
            written.append(out_dir / name)

    if ocfg.get("results_json", True):
        ping_rank = {id(r): i for i, r in enumerate(by_ping)}
        speed_rank = {id(r): i for i, r in enumerate(by_speed)}
        entries = []
        for r in by_ping:
            i, j = ping_rank[id(r)], speed_rank.get(id(r))
            entries.append({
                "rank_ping": i + 1,
                "rank_speed": j + 1 if j is not None else None,
                "protocol": r.config.protocol,
                "host": r.config.host,
                "port": r.config.port,
                "country_code": r.country_code,
                "country": r.country,
                "exit_ip": r.exit_ip,
                "ping_ms": r.ping_ms,
                "speed_mbps": r.speed_mbps,
                "source_channel": r.config.source,
                "link_by_ping": ping_links[i],
                "link_by_speed": speed_links[j] if j is not None else None,
            })
        payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "stats": stats,
            "configs": entries,
        }
        _write(out_dir / JSON_FILE, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        written.append(out_dir / JSON_FILE)

    log.info("Wrote %d links to %s and %d to %s", len(ping_links), PING, len(speed_links), SPEED)
    return written


# --------------------------------------------------------------------------- #
# Optional: commit + push (local workflow, --push)                            #
# --------------------------------------------------------------------------- #

def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)


def git_commit_push(root: Path, paths: Sequence[Path], message: str) -> bool:
    """Stage `paths`, commit if anything changed, push. Returns True on success."""
    rel = []
    for p in paths:
        try:
            rel.append(str(Path(p).resolve().relative_to(root.resolve())))
        except ValueError:
            log.warning("Not inside the repository, not committing: %s", p)
    if not rel:
        return False
    if _git(root, "rev-parse", "--is-inside-work-tree").returncode != 0:
        log.error("--push: %s is not a git repository", root)
        return False
    add = _git(root, "add", "--", *rel)
    if add.returncode != 0:
        log.error("git add failed: %s", add.stderr.strip())
        return False
    if _git(root, "diff", "--cached", "--quiet").returncode == 0:
        log.info("Nothing changed - nothing to commit")
        return True
    commit = _git(root, "commit", "-m", message)
    if commit.returncode != 0:
        log.error("git commit failed: %s", (commit.stderr or commit.stdout).strip())
        return False
    push = _git(root, "push")
    if push.returncode != 0:  # remote moved (e.g. the Actions run committed meanwhile)
        log.warning("git push failed (%s) - trying pull --rebase", push.stderr.strip())
        if _git(root, "pull", "--rebase").returncode == 0:
            push = _git(root, "push")
    if push.returncode != 0:
        log.error("git push failed: %s", push.stderr.strip())
        return False
    log.info("Pushed results to the remote repository")
    return True
