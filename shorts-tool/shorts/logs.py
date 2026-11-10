"""Логи: у консоль і у файл work/logs/<назва>_<дата>.log (UTF-8)."""

from __future__ import annotations

import logging
import sys
from datetime import datetime
from pathlib import Path


def setup_logging(name: str, log_dir: Path, verbose: bool = False) -> Path:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"{name}_{datetime.now():%Y-%m-%d_%H-%M-%S}.log"

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.DEBUG)

    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    root.addHandler(fh)

    if sys.stdout is not None:   # pythonw (вікно без консолі) не має stdout
        ch = logging.StreamHandler(sys.stdout)
        ch.setLevel(logging.DEBUG if verbose else logging.INFO)
        ch.setFormatter(logging.Formatter("%(message)s"))
        root.addHandler(ch)

    # Сторонні бібліотеки — тихіше.
    for noisy in ("easyocr", "PIL", "matplotlib", "numba", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return log_file
