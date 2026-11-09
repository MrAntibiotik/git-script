"""Завантаження налаштувань (config/settings.json) і профілів ігор (config/profiles/*.json).

Усі параметри живуть у JSON-файлах. Якщо якогось ключа у файлі немає —
береться значення за замовчуванням звідси, тож старі профілі не ламаються,
коли в нових версіях з'являються нові параметри.
Ключі, що починаються з «_» (наприклад «_опис»), — це коментарі, код їх ігнорує.
"""

from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path

from .errors import UserError

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
PROFILES_DIR = CONFIG_DIR / "profiles"
SETTINGS_FILE = CONFIG_DIR / "settings.json"

# ------------------------------------------------------------------ типові значення

DEFAULT_SETTINGS: dict = {
    "paths": {
        "ffmpeg": "auto",
        "ffprobe": "auto",
        "work_dir": "work",
        "output_dir": "output",
        "models_dir": "models",
    },
    "recording": {
        "mode": "mixed",
        "mixed": {"audio_track": 0},
        "separate": {
            "game_audio_track": 0,
            "mic_audio_track": 1,
            "webcam_file_suffix": "_webcam",
            "webcam_search_dirs": [],
        },
    },
    "processing": {"chunk_minutes": 10, "hwaccel": "cuda"},
    "ocr": {
        "languages": ["uk", "en"],
        "gpu": True,
        "digit_upscale": 2.0,
        "text_upscale": 1.5,
        "min_confidence": 0.25,
        "change_pixel_delta": 40,
        "change_fraction": 0.008,
        "refresh_s": 1.0,
    },
}

DEFAULT_PROFILE: dict = {
    "name": "Гра",
    "zones_confirmed": False,
    "zones": {},
    "signals": {"vision": False, "game_audio": True, "voice": True},
    "vision": {
        "sample_fps": 2,
        "time_correction_s": -0.25,
        "damage_counter": {
            "enabled": True,
            "max_jump": 3000,
            "max_value": 30000,
            "confirm_frames": 2,
            "reset_confirm_frames": 4,
            "resync_frames": 6,
        },
        "event_log": {
            "enabled": True,
            "kill_keywords": ["знищив", "знищено"],
            "keyword_ratio": 0.75,
            "memory_s": 20,
            "match_ratio": 0.75,
            "min_chars": 3,
            "confirm_frames": 1,
        },
        "hp": {
            "enabled": True,
            "use_text": True,
            "use_bar": True,
            "min_drop": 30,
            "bar_min_drop": 0.04,
            "confirm_frames": 2,
        },
        "end_screen": {
            "enabled": True,
            "check_every_s": 2.0,
            "scale": 0.6,
            "death_keywords": ["оцініть бій", "перемкнути камеру"],
            "battle_end_keywords": ["перемога", "поразка", "нічия"],
            "keyword_ratio": 0.8,
            "cooldown_s": 60,
        },
    },
    "battles": {"split_gap_s": 45, "merge_death_s": 20, "log_match_s": 2.0},
    "layout": {},
}

# Зони, які використовує зоровий детектор (решта — для макета).
VISION_ZONES = ["damage_counter", "event_log", "hp_text", "hp_bar", "end_screen"]


# ------------------------------------------------------------------ утиліти

def deep_merge(base: dict, override: dict) -> dict:
    """Рекурсивно накладає override на копію base."""
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def strip_comments(d):
    if isinstance(d, dict):
        return {k: strip_comments(v) for k, v in d.items() if not str(k).startswith("_")}
    if isinstance(d, list):
        return [strip_comments(x) for x in d]
    return d


def _read_json(path: Path) -> dict:
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            return json.load(f)
    except FileNotFoundError:
        raise UserError(f"Не знайдено файл {path}",
                        "Перевірте, що папку інструмента скопійовано повністю.")
    except json.JSONDecodeError as e:
        raise UserError(
            f"Помилка у файлі {path.name}, рядок {e.lineno}, позиція {e.colno}: {e.msg}",
            "Найчастіше це зайва або пропущена кома чи лапки. Відкрийте файл у Блокноті "
            "і виправте цей рядок (або поверніть копію .bak).")


def resolve_path(p: str | Path) -> Path:
    """Відносні шляхи рахуються від папки інструмента."""
    p = Path(p)
    return p if p.is_absolute() else (ROOT / p)


# ------------------------------------------------------------------ налаштування

def load_settings() -> dict:
    user = _read_json(SETTINGS_FILE) if SETTINGS_FILE.exists() else {}
    return deep_merge(DEFAULT_SETTINGS, strip_comments(user))


def work_dir(settings: dict) -> Path:
    d = resolve_path(settings["paths"]["work_dir"])
    d.mkdir(parents=True, exist_ok=True)
    return d


def output_dir(settings: dict) -> Path:
    d = resolve_path(settings["paths"]["output_dir"])
    d.mkdir(parents=True, exist_ok=True)
    return d


def models_dir(settings: dict) -> Path:
    d = resolve_path(settings["paths"]["models_dir"])
    d.mkdir(parents=True, exist_ok=True)
    return d


# ------------------------------------------------------------------ профілі

def list_profiles() -> list[str]:
    return sorted(p.stem for p in PROFILES_DIR.glob("*.json"))


def profile_path(name: str) -> Path:
    return PROFILES_DIR / f"{name}.json"


def load_profile_raw(name: str) -> dict:
    path = profile_path(name)
    if not path.exists():
        raise UserError(f"Профіль гри «{name}» не знайдено ({path}).",
                        f"Доступні профілі: {', '.join(list_profiles()) or 'немає'}.")
    return _read_json(path)


def load_profile(name: str) -> dict:
    prof = deep_merge(DEFAULT_PROFILE, strip_comments(load_profile_raw(name)))
    prof["id"] = name
    validate_zones(prof)
    return prof


def save_profile_raw(name: str, data: dict) -> Path:
    """Зберігає профіль, попередню версію кладе поруч як .bak."""
    path = profile_path(name)
    if path.exists():
        shutil.copy2(path, path.with_suffix(".json.bak"))
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    return path


def validate_zones(prof: dict) -> None:
    for key, z in (prof.get("zones") or {}).items():
        ok = (isinstance(z, list) and len(z) == 4
              and all(isinstance(v, (int, float)) for v in z)
              and 0 <= z[0] < 1 and 0 <= z[1] < 1 and 0 < z[2] <= 1 and 0 < z[3] <= 1
              and z[0] + z[2] <= 1.0001 and z[1] + z[3] <= 1.0001)
        if not ok:
            raise UserError(
                f"Зона «{key}» у профілі «{prof.get('id')}» задана неправильно: {z}",
                "Формат: [x, y, ширина, висота] у частках кадру від 0 до 1. "
                "Найпростіше — заново обвести зону через pick_zones.bat.")


def zone_px(zone: list[float], width: int, height: int) -> tuple[int, int, int, int]:
    """Частки кадру -> пікселі (x, y, w, h), обрізано по межах кадру, мінімум 2×2."""
    x = int(round(zone[0] * width))
    y = int(round(zone[1] * height))
    w = int(round(zone[2] * width))
    h = int(round(zone[3] * height))
    x = max(0, min(x, width - 2))
    y = max(0, min(y, height - 2))
    w = max(2, min(w, width - x))
    h = max(2, min(h, height - y))
    return x, y, w, h


def ask_profile(default: str | None = None) -> str:
    """Питає в консолі, який профіль гри використати."""
    names = list_profiles()
    if not names:
        raise UserError("У папці config/profiles немає жодного профілю гри.",
                        "Скопіюйте папку інструмента повністю.")
    print("\nДоступні профілі ігор:")
    for i, n in enumerate(names, 1):
        try:
            title = strip_comments(load_profile_raw(n)).get("name", n)
        except UserError:
            title = n
        print(f"  {i}. {title}  ({n})")
    hint = f" [Enter = {default}]" if default in names else ""
    while True:
        ans = input(f"Номер профілю{hint}: ").strip()
        if not ans and default in names:
            return default
        if ans.isdigit() and 1 <= int(ans) <= len(names):
            return names[int(ans) - 1]
        if ans in names:
            return ans
        print("  Не зрозумів. Введіть номер зі списку.")
