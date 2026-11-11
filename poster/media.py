"""ffprobe / ffmpeg: розміри відео і стиснута прев'ю-копія для Telegram (ліміт Bot API 50 МБ)."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

log = logging.getLogger("media")

TG_LIMIT = 49 * 1024 * 1024  # з невеликим запасом до 50 МБ


class MediaError(Exception):
    pass


def run_hidden(args: list[str], timeout: int = 900, cwd: str | None = None) -> subprocess.CompletedProcess:
    kw = {"cwd": cwd or None}
    if sys.platform == "win32":  # не показувати консольні вікна під pythonw
        kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        return subprocess.run(args, capture_output=True, timeout=timeout, **kw)
    except FileNotFoundError as e:
        raise MediaError(f"Не знайдено {args[0]} — встановіть ffmpeg і вкажіть шлях у config.json") from e
    except subprocess.TimeoutExpired as e:
        raise MediaError(f"{Path(args[0]).name}: перевищено час очікування") from e


def probe(path: str, ffprobe: str = "ffprobe") -> dict:
    """Повертає {'width', 'height', 'duration'} або {} якщо ffprobe недоступний."""
    try:
        r = run_hidden([ffprobe, "-v", "error", "-select_streams", "v:0",
                        "-show_entries", "stream=width,height:stream_tags=rotate:format=duration",
                        "-of", "json", str(path)], timeout=60)
        info = json.loads(r.stdout or b"{}")
        st = (info.get("streams") or [{}])[0]
        w, h = int(st.get("width") or 0), int(st.get("height") or 0)
        if str((st.get("tags") or {}).get("rotate", "0")) in ("90", "270", "-90"):
            w, h = h, w
        dur = float((info.get("format") or {}).get("duration") or 0)
        return {"width": w, "height": h, "duration": int(round(dur))}
    except (MediaError, ValueError) as e:
        log.warning("ffprobe недоступний або помилка (%s): %s", path, e)
        return {}


def telegram_copy(path: str, cfg: dict) -> str:
    """Оригінал, якщо ≤ 50 МБ; інакше — стиснута 720p-копія в data/previews."""
    if os.path.getsize(path) <= TG_LIMIT:
        return path
    cache = Path(cfg["paths"]["data"]) / "previews"
    cache.mkdir(parents=True, exist_ok=True)
    key = hashlib.md5(f"{path}|{os.path.getmtime(path)}".encode()).hexdigest()[:10]
    out = cache / f"{Path(path).stem}_{key}_720p.mp4"
    if out.exists() and 0 < out.stat().st_size <= TG_LIMIT:
        return str(out)

    ffmpeg = cfg["ffmpeg"]
    scale = "scale='if(gt(iw,ih),-2,720)':'if(gt(iw,ih),720,-2)'"
    base = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(path), "-vf", scale,
            "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart"]
    log.info("Файл %s > 50 МБ — стискаю прев'ю для Telegram", path)
    r = run_hidden(base + ["-crf", "28", str(out)])
    if r.returncode != 0:
        raise MediaError(f"ffmpeg: {r.stderr.decode(errors='ignore')[-300:]}")
    if out.stat().st_size > TG_LIMIT:
        dur = probe(path, cfg["ffprobe"]).get("duration") or 60
        kbps = max(300, int(TG_LIMIT * 8 * 0.9 / dur / 1000) - 96)
        r = run_hidden(base + ["-b:v", f"{kbps}k", "-maxrate", f"{kbps}k", "-bufsize", f"{kbps * 2}k", str(out)])
        if r.returncode != 0 or out.stat().st_size > TG_LIMIT:
            raise MediaError("Не вдалося стиснути відео до 50 МБ")
    return str(out)


def drop_previews(path: str, cfg: dict) -> None:
    cache = Path(cfg["paths"]["data"]) / "previews"
    for p in cache.glob(f"{Path(path).stem}_*_720p.mp4"):
        try:
            p.unlink()
        except OSError:
            pass
