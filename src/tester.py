"""Testing: ping + geo (phase 1, high concurrency) and speed (phase 2, low concurrency).

Why two phases?  Ping and geo are tiny requests, so many configs can be tested in
parallel. A speed test saturates the link: running dozens at once would just measure
how the connections share your bandwidth. So only the *survivors* of phase 1 are
speed-tested, a few at a time.

Every config gets its own sing-box process (see singbox.py). Nothing in here is
allowed to raise out of a worker: a bad config becomes a TestResult with a status
and an error message, never a crashed run.
"""
from __future__ import annotations

import logging
import statistics
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, TypeVar

import requests

from . import geo
from .models import ProxyConfig, TestResult
from .singbox import CoreError, CoreProcess, UnsupportedConfig, build_outbound, kill_all

log = logging.getLogger("v2s.tester")

T = TypeVar("T")
_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36"}


# --------------------------------------------------------------------------- #
# Measurements (all go through the local proxy)                               #
# --------------------------------------------------------------------------- #

def _session(proxies: Dict[str, str]) -> requests.Session:
    session = requests.Session()
    session.trust_env = False          # ignore HTTP(S)_PROXY / NO_PROXY / .netrc of the host
    session.proxies.update(proxies)
    session.headers.update(_UA)
    return session


def _timed_get(proxies: Dict[str, str], url: str, timeout: float, expected: int) -> Optional[float]:
    """One request over a fresh connection. Returns milliseconds, or None on any failure."""
    start = time.perf_counter()
    try:
        with _session(proxies) as session:
            resp = session.get(url, timeout=timeout, allow_redirects=False)
    except requests.RequestException:
        return None
    if resp.status_code != expected:   # e.g. a captive portal answering 200 instead of 204
        return None
    return (time.perf_counter() - start) * 1000.0


def measure_ping(proxies: Dict[str, str], pcfg: Dict[str, Any]) -> Optional[int]:
    """Average latency in ms over `attempts` requests, or None if the config is dead.

    The first `warmup` request(s) are not timed; if the very first request fails the
    config is declared dead immediately (so dead configs cost one timeout, not four).
    """
    url, timeout = str(pcfg["url"]), float(pcfg["timeout"])
    expected = int(pcfg.get("expected_status", 204))
    warmup = max(0, int(pcfg.get("warmup", 1)))
    attempts = max(1, int(pcfg.get("attempts", 3)))
    min_success = min(attempts, max(1, int(pcfg.get("min_success", 1))))

    samples: List[float] = []
    for i in range(warmup + attempts):
        ms = _timed_get(proxies, url, timeout, expected)
        if ms is None:
            if i == 0:
                return None
            continue
        if i >= warmup:
            samples.append(ms)
    if len(samples) < min_success:
        return None
    return int(round(statistics.mean(samples)))


def _download_once(proxies: Dict[str, str], url: str, scfg: Dict[str, Any]) -> Optional[float]:
    """Stream one download, return Mbps (megabits/s) or None.

    Timing starts at the first received chunk so connection set-up / TTFB does not
    drag the figure down. A download cut off by `timeout` still yields a speed from
    the bytes received so far.
    """
    max_seconds = float(scfg.get("timeout", 15))
    started = time.perf_counter()
    first_at: Optional[float] = None
    last_at = 0.0
    counted = 0
    try:
        with _session(proxies) as session:
            with session.get(url, stream=True,
                             timeout=(float(scfg.get("connect_timeout", 8)), 10)) as resp:
                if resp.status_code != 200:
                    return None
                for chunk in resp.iter_content(chunk_size=32768):
                    now = time.perf_counter()
                    if first_at is None:
                        first_at = now        # first chunk starts the clock and is not counted
                        last_at = now
                        continue
                    counted += len(chunk)
                    last_at = now
                    if now - started >= max_seconds:
                        break
    except requests.RequestException:
        pass  # keep whatever was downloaded before the connection broke
    if first_at is None or counted < 64 * 1024:
        return None
    elapsed = max(last_at - first_at, 1e-3)
    return round(counted * 8 / elapsed / 1_000_000, 2)


def measure_speed(proxies: Dict[str, str], scfg: Dict[str, Any]) -> Optional[float]:
    for url in scfg.get("urls", []):
        mbps = _download_once(proxies, str(url), scfg)
        if mbps is not None:
            return mbps
    return None


# --------------------------------------------------------------------------- #
# Generic worker pool                                                         #
# --------------------------------------------------------------------------- #

def _run_pool(fn: Callable[[T], Any], items: Sequence[T], workers: int, label: str,
              every: int = 25) -> None:
    """Run fn(item) for every item. fn mutates the item in place and must not raise;
    if it does anyway the exception is logged and the run continues."""
    if not items:
        return
    executor = ThreadPoolExecutor(max_workers=max(1, min(workers, len(items))),
                                  thread_name_prefix=label)
    futures: Dict[Future, int] = {executor.submit(fn, item): n for n, item in enumerate(items)}
    done = 0
    try:
        for future in as_completed(futures):
            try:
                future.result()
            except Exception as exc:  # last line of defence
                log.error("%s: worker crashed: %r", label, exc)
            done += 1
            if done % every == 0 or done == len(items):
                log.info("%s: %d/%d", label, done, len(items))
    except KeyboardInterrupt:
        log.warning("Interrupted - stopping workers and killing core processes")
        executor.shutdown(wait=False, cancel_futures=True)
        kill_all()
        raise
    executor.shutdown(wait=True)


# --------------------------------------------------------------------------- #
# The tester                                                                  #
# --------------------------------------------------------------------------- #

class Tester:
    """Runs the two test phases over a list of ProxyConfig objects."""

    def __init__(self, cfg: Dict[str, Any], binary: Path):
        self.t = cfg["testing"]
        self.binary = Path(binary)
        budget_min = float(self.t.get("max_runtime_minutes", 0) or 0)
        started = time.monotonic()
        # Phase 1 may use at most 60% of the budget so phase 2 always gets a chance.
        self.ping_deadline = started + 0.6 * budget_min * 60 if budget_min > 0 else None
        self.final_deadline = started + budget_min * 60 if budget_min > 0 else None
        self._lock = threading.Lock()

    # -- helpers ---------------------------------------------------------- #

    @staticmethod
    def _expired(deadline: Optional[float]) -> bool:
        return deadline is not None and time.monotonic() > deadline

    def _core(self, outbound: Dict[str, Any]) -> CoreProcess:
        return CoreProcess(self.binary, outbound, float(self.t.get("startup_timeout", 6)))

    # -- phase 1: ping + geo ---------------------------------------------- #

    def _phase1(self, res: TestResult) -> None:
        if self._expired(self.ping_deadline):
            res.status, res.error = "skipped", "time budget exhausted"
            return
        try:
            outbound = build_outbound(res.config)
        except UnsupportedConfig as exc:
            res.status, res.error = "unsupported", str(exc)
            return
        except Exception as exc:
            res.status, res.error = "error", f"cannot build outbound: {exc!r}"
            return

        try:
            with self._core(outbound) as core:
                ping = measure_ping(core.proxies, self.t["ping"])
                if ping is None:
                    res.status, res.error = "dead", "no valid response"
                    return
                if ping > int(self.t["ping"].get("max_ping_ms", 3000)):
                    res.status, res.error = "filtered", f"ping {ping}ms too high"
                    return
                res.ping_ms, res.alive, res.status = ping, True, "ok"
                if self.t["geo"].get("enabled", True):
                    info = geo.lookup(core.proxies, self.t["geo"])
                    if info:
                        res.country_code, res.country, res.exit_ip = info.country_code, info.country, info.ip
        except CoreError as exc:
            res.status, res.error = "core-error", str(exc)
        except Exception as exc:
            res.status, res.error = "error", repr(exc)
            log.debug("phase 1 error for %s:%s", res.config.host, res.config.port, exc_info=True)

    # -- phase 2: speed --------------------------------------------------- #

    def _phase2(self, res: TestResult) -> None:
        if self._expired(self.final_deadline):
            res.extra["speed_skipped"] = True
            return
        scfg = self.t["speed"]
        try:
            with self._core(build_outbound(res.config)) as core:
                res.speed_mbps = measure_speed(core.proxies, scfg)
        except Exception as exc:
            log.debug("speed test error for %s:%s: %r", res.config.host, res.config.port, exc)
            res.speed_mbps = None
        if res.speed_mbps is None:
            if scfg.get("drop_on_fail", True):
                res.alive, res.status, res.error = False, "filtered", "speed test failed"
            return
        min_mbps = float(scfg.get("min_mbps", 0) or 0)
        if min_mbps and res.speed_mbps < min_mbps:
            res.alive, res.status, res.error = False, "filtered", f"{res.speed_mbps} Mbps below minimum"

    # -- public ----------------------------------------------------------- #

    def run(self, configs: List[ProxyConfig]) -> List[TestResult]:
        results = [TestResult(config=c) for c in configs]
        log.info("Phase 1/2: ping + geo for %d configs (concurrency %s)",
                 len(results), self.t["concurrency"])
        _run_pool(self._phase1, results, int(self.t["concurrency"]), "ping")

        alive = sorted((r for r in results if r.alive), key=lambda r: r.ping_ms or 0)
        log.info("Phase 1 done: %d alive of %d", len(alive), len(results))

        scfg = self.t["speed"]
        if scfg.get("enabled", True) and alive:
            top_n = int(scfg.get("top_n", 0) or 0)
            targets = alive[:top_n] if top_n else alive
            log.info("Phase 2/2: speed test for %d configs (concurrency %s)",
                     len(targets), self.t["speed_concurrency"])
            _run_pool(self._phase2, targets, int(self.t["speed_concurrency"]), "speed", every=10)
        return results
