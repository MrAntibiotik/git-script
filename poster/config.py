"""Завантаження налаштувань: config.json (параметри) + .env (секрети)."""
from __future__ import annotations

import copy
import json
import logging
import os
import re
import sys
import time
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from zoneinfo import ZoneInfo

BASE_DIR = Path(__file__).resolve().parent

PLATFORMS = ["youtube", "tiktok", "instagram", "facebook"]
SHORT = {"youtube": "YT", "tiktok": "TT", "instagram": "IG", "facebook": "FB"}
NAMES = {"youtube": "YouTube", "tiktok": "TikTok", "instagram": "Instagram", "facebook": "Facebook"}

DEFAULTS: dict = {
    "timezone": "Europe/Kyiv",
    "slots": ["10:00", "13:00", "16:00", "19:00", "21:00"],
    "notify_before_minutes": 60,
    "min_notify_lead_minutes": 5,
    "publish_on_approve": False,
    "catchup_minutes": 120,
    "max_skips": 3,
    "ready_status": "ready_to_post",
    "scan_interval_seconds": 60,
    "regenerate_command": [],
    "regenerate_cwd": "",
    "ffmpeg": "ffmpeg",
    "ffprobe": "ffprobe",
    "dry_run": False,
    "lock_port": 47613,
    "graph_api_version": "v23.0",
    "paths": {
        "ready": "ready",
        "posted": "posted",
        "skipped": "skipped",
        "data": "data",
        "tokens": "tokens",
        "log": "log.txt",
    },
    "telegram": {"bot_token": "", "chat_id": ""},
    "retry": {"attempts": 3, "pause_seconds": 120},
    "reports": {"daily_time": "22:30", "stats_after_hours": 24},
    "platforms": {
        "youtube": {
            "enabled": True,
            "client_secrets_file": "client_secret.json",
            "privacy": "public",
            "category_id": "20",
            "language": "uk",
            "add_shorts_hashtag": True,
            "notify_subscribers": True,
        },
        "instagram": {"enabled": True, "share_to_feed": True},
        "facebook": {"enabled": True},
        "tiktok": {
            "enabled": True,
            "public": False,
            "redirect_uri": "http://localhost:8765/callback/",
            "disable_comment": False,
            "disable_duet": False,
            "disable_stitch": False,
        },
    },
}

# Секрети: ім'я змінної в .env -> шлях у config.json (запасний варіант)
SECRETS = {
    "TELEGRAM_BOT_TOKEN": ("telegram", "bot_token"),
    "TELEGRAM_CHAT_ID": ("telegram", "chat_id"),
    "META_APP_ID": ("meta", "app_id"),
    "META_APP_SECRET": ("meta", "app_secret"),
    "META_PAGE_TOKEN": ("meta", "page_token"),
    "META_IG_TOKEN": ("meta", "ig_token"),
    "FB_PAGE_ID": ("meta", "page_id"),
    "IG_USER_ID": ("meta", "ig_user_id"),
    "TIKTOK_CLIENT_KEY": ("tiktok", "client_key"),
    "TIKTOK_CLIENT_SECRET": ("tiktok", "client_secret"),
}


class ConfigError(Exception):
    pass


def _merge(base: dict, extra: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def _dig(d: dict, path: tuple):
    for p in path:
        if not isinstance(d, dict):
            return None
        d = d.get(p)
    return d


def load_config(path: str | os.PathLike | None = None) -> dict:
    path = Path(path or os.environ.get("POSTER_CONFIG") or BASE_DIR / "config.json").resolve()
    root = path.parent
    try:
        from dotenv import load_dotenv

        load_dotenv(root / ".env")
    except ImportError:  # pragma: no cover
        pass
    if not path.exists():
        raise ConfigError(
            f"Не знайдено {path}. Скопіюйте config.example.json у config.json і заповніть."
        )
    with open(path, encoding="utf-8") as f:
        try:
            user = json.load(f)
        except json.JSONDecodeError as e:
            raise ConfigError(f"Помилка в {path.name}: {e}") from e
    cfg = _merge(DEFAULTS, user)
    cfg["config_path"] = str(path)
    cfg["root"] = str(root)
    cfg["env_path"] = str(root / ".env")

    for key, val in cfg["paths"].items():
        p = Path(os.path.expandvars(str(val)))
        cfg["paths"][key] = (p if p.is_absolute() else root / p).resolve()
    yt = cfg["platforms"]["youtube"]
    p = Path(yt["client_secrets_file"])
    yt["client_secrets_file"] = str(p if p.is_absolute() else root / p)

    cfg["secrets"] = {
        name: (os.environ.get(name) or str(_dig(user, path_) or "")).strip()
        for name, path_ in SECRETS.items()
    }

    slots = cfg["slots"]
    if not isinstance(slots, list) or not 1 <= len(slots) <= 6:
        raise ConfigError("slots: потрібно від 1 до 6 значень на кшталт \"13:00\"")
    for s in slots:
        if not re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", str(s)):
            raise ConfigError(f"slots: неправильний час «{s}»")
    cfg["slots"] = sorted({_hm(s) for s in slots})
    try:
        cfg["reports"]["daily_time"] = _hm(cfg["reports"]["daily_time"])
    except ValueError as e:
        raise ConfigError("reports.daily_time: очікується час «HH:MM»") from e
    cfg["tz"] = ZoneInfo(cfg["timezone"])
    return cfg


def _hm(value) -> str:
    h, m = map(int, str(value).strip().split(":"))
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError(value)
    return f"{h:02d}:{m:02d}"


def enabled_platforms(cfg: dict) -> list[str]:
    return [p for p in PLATFORMS if cfg["platforms"].get(p, {}).get("enabled")]


def setup_logging(cfg: dict, verbose: bool = False) -> None:
    log_path = Path(cfg["paths"]["log"])
    log_path.parent.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    for h in list(root.handlers):
        root.removeHandler(h)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    fh = RotatingFileHandler(log_path, maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    fh.setFormatter(fmt)
    fh.setLevel(logging.DEBUG)
    root.addHandler(fh)
    if sys.stderr is not None:  # під pythonw консолі немає
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        sh.setLevel(logging.DEBUG if verbose else logging.INFO)
        root.addHandler(sh)
    for noisy in ("urllib3", "googleapiclient", "google_auth_httplib2", "google.auth"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


# ---------- час ----------

FAKE_NOW: int | None = None  # лише для тестів


def now_ts() -> int:
    return FAKE_NOW if FAKE_NOW is not None else int(time.time())


def local_dt(cfg: dict, ts: int) -> datetime:
    return datetime.fromtimestamp(ts, cfg["tz"])


def fmt_slot(cfg: dict, ts: int, now: int | None = None) -> str:
    dt = local_dt(cfg, ts)
    today = local_dt(cfg, now or now_ts()).date()
    return dt.strftime("%H:%M") if dt.date() == today else dt.strftime("%d.%m %H:%M")
