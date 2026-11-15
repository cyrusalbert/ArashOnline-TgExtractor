"""Logging set-up: one consistent format for console (and optionally a file)."""
from __future__ import annotations

import logging
import sys


def setup_logging(level: str = "INFO", log_file: str = "") -> None:
    """Configure the 'v2s' logger tree. Safe to call more than once."""
    root = logging.getLogger("v2s")
    root.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    root.handlers.clear()
    root.propagate = False

    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S")

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)

    if log_file:
        try:
            file_handler = logging.FileHandler(log_file, encoding="utf-8")
            file_handler.setFormatter(fmt)
            root.addHandler(file_handler)
        except OSError as exc:  # logging must never stop the run
            root.warning("Cannot open log file %s: %s", log_file, exc)
