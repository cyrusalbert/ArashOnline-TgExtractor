"""Download and cache the sing-box binary for the current OS / CPU.

Used by both workflows:
  * GitHub Actions calls   python -m src.core_installer   in its own step
  * run_local.py calls     ensure_core()                   automatically

Resolution order: core.path (explicit) -> cached bin/<binary> -> download.
"""
from __future__ import annotations

import argparse
import logging
import os
import platform
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

log = logging.getLogger("v2s.core")

REPO = "SagerNet/sing-box"
API = f"https://api.github.com/repos/{REPO}/releases"
_ARCHES = {"x86_64": "amd64", "amd64": "amd64", "x64": "amd64", "aarch64": "arm64", "arm64": "arm64",
           "armv7l": "armv7", "armv7": "armv7", "i386": "386", "i686": "386", "x86": "386"}


class InstallError(RuntimeError):
    pass


def platform_tags() -> Tuple[str, str]:
    """('linux'|'darwin'|'windows', 'amd64'|'arm64'|...) as used in release asset names."""
    system = platform.system().lower()
    arch = _ARCHES.get(platform.machine().lower())
    if system not in ("linux", "darwin", "windows") or arch is None:
        raise InstallError(f"Unsupported platform: {platform.system()} / {platform.machine()}")
    return system, arch


def binary_name() -> str:
    return "sing-box.exe" if platform.system().lower() == "windows" else "sing-box"


def _works(binary: Path) -> bool:
    try:
        proc = subprocess.run([str(binary), "version"], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0 and "sing-box" in proc.stdout.lower()


def _headers() -> Dict[str, str]:
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "v2ray-telegram-scraper"}
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _resolve_asset(version: str, fallback: str, system: str, arch: str) -> Tuple[str, str]:
    """Return (resolved_version, download_url). Falls back to a pinned version on API errors."""
    ext = "zip" if system == "windows" else "tar.gz"
    wanted_ver: Optional[str] = None if version in ("", "latest") else version.lstrip("v")
    endpoint = f"{API}/latest" if wanted_ver is None else f"{API}/tags/v{wanted_ver}"
    try:
        resp = requests.get(endpoint, headers=_headers(), timeout=20)
        resp.raise_for_status()
        release = resp.json()
        tag = str(release["tag_name"]).lstrip("v")
        exact = f"sing-box-{tag}-{system}-{arch}.{ext}"
        assets: List[Dict[str, Any]] = release.get("assets", [])
        for asset in assets:
            if asset.get("name") == exact:
                return tag, asset["browser_download_url"]
        for asset in assets:  # tolerate naming drift, but never pick legacy/android/glibc variants
            name = str(asset.get("name", ""))
            if f"-{system}-{arch}." in name and name.endswith(ext) \
                    and not any(x in name for x in ("legacy", "glibc", "android", "purego")):
                return tag, asset["browser_download_url"]
        raise InstallError(f"no {system}/{arch} asset in release v{tag}")
    except (requests.RequestException, ValueError, KeyError, InstallError) as exc:
        ver = wanted_ver or fallback.lstrip("v")
        log.warning("Release lookup failed (%s) - falling back to pinned v%s", exc, ver)
        url = f"https://github.com/{REPO}/releases/download/v{ver}/sing-box-{ver}-{system}-{arch}.{ext}"
        return ver, url


def _download(url: str, dest: Path) -> None:
    last: Optional[Exception] = None
    for attempt in range(1, 4):
        try:
            with requests.get(url, stream=True, timeout=(15, 60), headers=_headers()) as resp:
                resp.raise_for_status()
                with open(dest, "wb") as handle:
                    for chunk in resp.iter_content(chunk_size=1 << 16):
                        handle.write(chunk)
            return
        except requests.RequestException as exc:
            last = exc
            log.warning("Download failed (attempt %d/3): %s", attempt, exc)
    raise InstallError(f"could not download {url}: {last}")


def _extract(archive: Path, bin_dir: Path) -> None:
    """Extract files flat into bin_dir. Only the *basename* of each member is used, so a
    malicious archive can never write outside bin_dir (no path traversal)."""
    bin_dir.mkdir(parents=True, exist_ok=True)

    def save(name: str, data: bytes) -> None:
        base = os.path.basename(name)
        if base:
            (bin_dir / base).write_bytes(data)

    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as zf:
            for info in zf.infolist():
                if not info.is_dir():
                    save(info.filename, zf.read(info))
    else:
        with tarfile.open(archive, "r:gz") as tf:
            for member in tf:
                if member.isfile():
                    handle = tf.extractfile(member)
                    if handle is not None:
                        save(member.name, handle.read())


def ensure_core(cfg: Dict[str, Any], root: Path, force: bool = False) -> Path:
    """Return a working sing-box binary path, downloading it if necessary."""
    ccfg = cfg["core"]
    if ccfg.get("path"):
        explicit = Path(str(ccfg["path"])).expanduser()
        if explicit.is_file():
            return explicit
        raise InstallError(f"core.path does not exist: {explicit}")

    bin_dir = root / str(ccfg["bin_dir"])
    binary = bin_dir / binary_name()
    if binary.is_file() and not force and _works(binary):
        log.info("Using cached core: %s", binary)
        return binary

    system, arch = platform_tags()
    version, url = _resolve_asset(str(ccfg.get("version", "latest")),
                                  str(ccfg.get("fallback_version", "1.12.4")), system, arch)
    log.info("Downloading sing-box v%s for %s/%s", version, system, arch)
    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / ("core.zip" if system == "windows" else "core.tar.gz")
        _download(url, archive)
        try:
            _extract(archive, bin_dir)
        except (tarfile.TarError, zipfile.BadZipFile, OSError) as exc:
            raise InstallError(f"cannot unpack archive: {exc}") from exc

    if not binary.is_file():
        raise InstallError("archive did not contain a sing-box binary")
    binary.chmod(binary.stat().st_mode | 0o755)
    if not _works(binary):
        raise InstallError(f"{binary} downloaded but does not run on this machine")
    log.info("sing-box ready: %s", binary)
    return binary


def main(argv: Optional[List[str]] = None) -> int:
    from .config import load_config
    from .logger import setup_logging

    root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description="Download the sing-box binary into bin/")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--force", action="store_true", help="re-download even if cached")
    args = parser.parse_args(argv)
    setup_logging("INFO")
    cfg = load_config(root / args.config)
    try:
        ensure_core(cfg, root, force=args.force)
    except InstallError as exc:
        log.error("%s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
