"""Робота з ffmpeg/ffprobe: пошук програм, інформація про файл, потік кадрів.

Оригінальні записи тільки читаються, нічого не змінюється.
Кадри йдуть потоком через pipe (по одному), тому пам'ять не росте
навіть на 8-годинних записах.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import numpy as np

from .config import ROOT, resolve_path
from .errors import UserError

log = logging.getLogger("media")

_NO_WINDOW = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW


# ------------------------------------------------------------------ пошук ffmpeg

def find_tool(settings: dict, name: str) -> str:
    """Шукає ffmpeg/ffprobe: налаштування -> папка ffmpeg поруч -> C:/ffmpeg -> PATH."""
    exe = name + (".exe" if os.name == "nt" else "")
    configured = (settings.get("paths") or {}).get(name, "auto")
    if configured and configured != "auto":
        p = resolve_path(configured)
        if p.is_dir():
            p = p / exe
        if p.exists():
            return str(p)
        raise UserError(f"У settings.json для «{name}» вказано шлях {p}, але файлу там немає.",
                        f'Виправте шлях або поставте "auto".')

    candidates = [ROOT / "ffmpeg" / "bin" / exe]
    candidates += sorted((ROOT / "ffmpeg").glob(f"*/bin/{exe}"))
    candidates += [Path("C:/ffmpeg/bin") / exe]
    for c in candidates:
        if c.exists():
            return str(c)
    found = shutil.which(name)
    if found:
        return found
    raise UserError(
        f"Не знайдено {name}.",
        "Завантажте ffmpeg (див. README, крок 2) і розпакуйте в папку ffmpeg "
        "всередині папки інструмента, щоб вийшло ffmpeg\\...\\bin\\ffmpeg.exe.")


def run(cmd: list[str], check: bool = True, timeout: float | None = None) -> subprocess.CompletedProcess:
    log.debug("RUN %s", " ".join(cmd))
    res = subprocess.run(cmd, capture_output=True, timeout=timeout, creationflags=_NO_WINDOW)
    if check and res.returncode != 0:
        err = res.stderr.decode("utf-8", "replace")[-1500:]
        raise UserError(f"Команда {Path(cmd[0]).name} завершилась з помилкою:\n{err}",
                        "Перевірте, що файл не пошкоджений і не відкритий в іншій програмі.")
    return res


# ------------------------------------------------------------------ інформація про файл

@dataclass
class VideoInfo:
    path: Path
    duration: float
    width: int
    height: int
    fps: float
    audio_streams: int
    video_codec: str


def probe(settings: dict, path: str | Path) -> VideoInfo:
    path = Path(path)
    if not path.exists():
        raise UserError(f"Файл не знайдено: {path}", "Перевірте шлях до запису.")
    ffprobe = find_tool(settings, "ffprobe")
    res = run([ffprobe, "-v", "error", "-print_format", "json",
               "-show_streams", "-show_format", str(path)])
    data = json.loads(res.stdout.decode("utf-8", "replace"))
    vstreams = [s for s in data.get("streams", []) if s.get("codec_type") == "video"]
    astreams = [s for s in data.get("streams", []) if s.get("codec_type") == "audio"]
    if not vstreams:
        raise UserError(f"У файлі {path.name} немає відео.", "Оберіть файл запису з відео.")
    v = vstreams[0]
    rate = v.get("avg_frame_rate") or v.get("r_frame_rate") or "60/1"
    try:
        num, den = rate.split("/")
        fps = float(num) / float(den) if float(den) else 60.0
    except Exception:
        fps = 60.0
    duration = float(data.get("format", {}).get("duration") or v.get("duration") or 0)
    if duration <= 0:
        raise UserError(f"Не вдалося визначити тривалість {path.name}.",
                        "Якщо запис обірвався (OBS аварійно закрився), зробіть Remux у OBS "
                        "(Файл → Ремукс записів) і спробуйте знову.")
    return VideoInfo(path=path, duration=duration, width=int(v["width"]), height=int(v["height"]),
                     fps=fps, audio_streams=len(astreams), video_codec=v.get("codec_name", "?"))


# ------------------------------------------------------------------ кадри

def extract_frame(settings: dict, path: str | Path, t: float, out_png: Path) -> Path:
    ffmpeg = find_tool(settings, "ffmpeg")
    out_png.parent.mkdir(parents=True, exist_ok=True)
    run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{t:.3f}", "-i", str(path),
         "-frames:v", "1", str(out_png)])
    if not out_png.exists():
        raise UserError(f"Не вдалося взяти кадр на {t:.1f} с.", "Спробуйте інший час (менший за тривалість).")
    return out_png


class _StderrDrain(threading.Thread):
    def __init__(self, stream):
        super().__init__(daemon=True)
        self.stream = stream
        self.chunks: list[bytes] = []

    def run(self):
        for line in iter(self.stream.readline, b""):
            self.chunks.append(line)
            if len(self.chunks) > 200:
                self.chunks = self.chunks[-100:]

    def text(self) -> str:
        return b"".join(self.chunks).decode("utf-8", "replace")


_hwaccel_broken = False


def iter_frames(settings: dict, info: VideoInfo, start: float, duration: float, fps: float,
                crop: tuple[int, int, int, int] | None = None) -> Iterator[tuple[float, np.ndarray]]:
    """Видає (час_у_секундах, кадр BGR) з частотою fps на відрізку [start, start+duration).

    crop=(x, y, w, h) — ffmpeg одразу вирізає лише потрібну частину кадру.
    Спершу пробує декодування на відеокарті (NVDEC), якщо не вийшло — на процесорі.
    """
    global _hwaccel_broken
    hw = (settings.get("processing", {}).get("hwaccel") or "").strip()
    attempts = [hw, ""] if hw and not _hwaccel_broken else [""]
    for attempt, accel in enumerate(attempts):
        got_any = False
        try:
            for item in _iter_frames_once(settings, info, start, duration, fps, crop, accel):
                got_any = True
                yield item
            return
        except _DecodeFailed as e:
            if got_any or attempt == len(attempts) - 1:
                raise UserError(f"ffmpeg не зміг прочитати відео {info.path.name}:\n{e}",
                                "Перевірте файл (чи відкривається він у плеєрі). Якщо запис "
                                "у .mkv обірвався — зробіть Remux в OBS.")
            log.warning("Декодування на відеокарті (%s) не вдалося, перемикаюсь на процесор (повільніше).", accel)
            _hwaccel_broken = True


class _DecodeFailed(Exception):
    pass


def _iter_frames_once(settings, info, start, duration, fps, crop, accel):
    ffmpeg = find_tool(settings, "ffmpeg")
    if crop:
        cx, cy, cw, ch = crop
        cw -= cw % 2
        ch -= ch % 2
        out_w, out_h = cw, ch
        vf = f"fps={fps},crop={cw}:{ch}:{cx}:{cy}"
    else:
        out_w, out_h = info.width, info.height
        vf = f"fps={fps}"
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin"]
    if accel:
        cmd += ["-hwaccel", accel]
    cmd += ["-ss", f"{start:.3f}", "-i", str(info.path), "-t", f"{duration:.3f}",
            "-an", "-sn", "-dn", "-vf", vf, "-pix_fmt", "bgr24", "-f", "rawvideo", "pipe:1"]
    log.debug("FRAMES %s", " ".join(cmd))
    frame_bytes = out_w * out_h * 3
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            bufsize=frame_bytes * 2, creationflags=_NO_WINDOW)
    drain = _StderrDrain(proc.stderr)
    drain.start()
    n = 0
    try:
        while True:
            buf = proc.stdout.read(frame_bytes)
            if not buf or len(buf) < frame_bytes:
                break
            frame = np.frombuffer(buf, dtype=np.uint8).reshape(out_h, out_w, 3)
            yield start + n / fps, frame
            n += 1
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        drain.join(timeout=2)
    if n == 0 and proc.returncode not in (0, None):
        raise _DecodeFailed(drain.text()[-1500:] or f"код {proc.returncode}")
