"""Субтитри: whisper.cpp локально (CUDA), українська мова, з доріжки голосу.

Щоб модель завантажувалась один раз, усі шматки всіх шортсів склеюються в один WAV
(з 2 с тиші між ними), розпізнаються одним запуском, а потім слова розкладаються
назад по шортсах. Далі слова збираються в короткі фрази (1–2 рядки), додаються
емодзі (крик/сміх у цей момент, ключові слова) і пишуться SRT-файли.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from . import events as E
from .audio import SR_VOICE, AudioChunk
from .config import ROOT, resolve_path
from .errors import UserError
from .selection import Short, SpeechMap

log = logging.getLogger("subtitles")
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
GAP_S = 2.0


@dataclass
class Word:
    text: str
    start: float   # час у записі
    end: float


@dataclass
class Cue:
    start: float   # час у шортсі
    end: float
    text: str


# ------------------------------------------------------------------ whisper.cpp

def find_whisper(settings: dict) -> Path:
    cfg = settings["whisper"]["cli"]
    exe = "whisper-cli.exe" if os.name == "nt" else "whisper-cli"
    if cfg and cfg != "auto":
        p = resolve_path(cfg)
        if p.exists():
            return p
        raise UserError(f"whisper.cpp не знайдено за шляхом {p}", 'Виправте whisper.cli у settings.json або поставте "auto".')
    for pattern in (exe, "main.exe" if os.name == "nt" else "main"):
        found = sorted((ROOT / "whisper").rglob(pattern))
        if found:
            return found[0]
    from shutil import which
    w = which("whisper-cli")
    if w:
        return Path(w)
    raise UserError("Не знайдено whisper.cpp (whisper-cli.exe).", "Запустіть setup_whisper.bat (README, етап 5).")


def whisper_model(settings: dict) -> Path:
    p = resolve_path(settings["whisper"]["model"])
    if not p.exists():
        raise UserError(f"Не знайдено модель whisper: {p}", "Запустіть setup_whisper.bat — він завантажить модель.")
    return p


def run_whisper(settings: dict, wav: Path, out_base: Path, gpu: bool | None = None) -> list[dict]:
    w = settings["whisper"]
    exe = find_whisper(settings)
    model = whisper_model(settings)
    use_gpu = w["gpu"] if gpu is None else gpu
    cmd = [str(exe), "-m", str(model), "-f", str(wav), "-l", w["language"], "-ml", "1", "-sow",
           "-oj", "-of", str(out_base), "-t", str(w["threads"]), "-bs", str(w["beam_size"]), "-np"]
    if w.get("prompt"):
        cmd += ["--prompt", w["prompt"]]
    if not use_gpu:
        cmd += ["-ng"]
    log.debug("WHISPER %s", " ".join(cmd))
    res = subprocess.run(cmd, capture_output=True, creationflags=_NO_WINDOW, cwd=str(exe.parent))
    jpath = Path(str(out_base) + ".json")
    if res.returncode != 0 or not jpath.exists():
        err = (res.stderr or b"").decode("utf-8", "replace")[-1500:]
        if use_gpu:
            log.warning("whisper.cpp на відеокарті не спрацював — пробую на процесорі. %s", err.strip()[-300:])
            return run_whisper(settings, wav, out_base, gpu=False)
        raise UserError(f"whisper.cpp завершився з помилкою:\n{err}",
                        "Перевірте setup_whisper.bat / check_setup.bat. Шортси буде зібрано без субтитрів.")
    data = json.loads(jpath.read_bytes().decode("utf-8", "replace"))
    return data.get("transcription", [])


# ------------------------------------------------------------------ звук для whisper

def _voice_audio(chunks: list[AudioChunk], a: float, b: float) -> np.ndarray:
    parts = []
    for ch in chunks:
        if ch.c1 <= a or ch.c0 >= b:
            continue
        y, sr = ch.load("voice")
        s0 = int(max(0.0, a - ch.c0) * sr)
        s1 = int((min(b, ch.c1) - ch.c0) * sr)
        parts.append(y[s0:s1])
    return np.concatenate(parts) if parts else np.zeros(int((b - a) * SR_VOICE), np.float32)


def transcribe_shorts(settings: dict, shorts: list[Short], chunks: list[AudioChunk], work: Path) -> dict[int, list[Word]]:
    """Повертає {індекс_шортсу: [слова з часом у записі]}."""
    import soundfile as sf
    pieces = []      # (зсув у WAV, початок у записі, тривалість, індекс шортсу)
    audio = []
    pos = 0.0
    gap = np.zeros(int(GAP_S * SR_VOICE), np.float32)
    for sh in shorts:
        for seg in sh.segments:
            y = _voice_audio(chunks, seg.start, seg.end)
            # підсилюємо тихий голос до нормального рівня (whisper так краще чує)
            peak = float(np.abs(y).max()) if y.size else 0
            if 1e-4 < peak < 0.5:
                y = y * (0.7 / peak)
            audio += [y, gap]
            pieces.append((pos, seg.start, len(y) / SR_VOICE, sh.index))
            pos += len(y) / SR_VOICE + GAP_S
    if not pieces:
        return {}
    work.mkdir(parents=True, exist_ok=True)
    wav = work / "whisper_input.wav"
    sf.write(str(wav), np.concatenate(audio), SR_VOICE, subtype="PCM_16")
    log.info("  Розпізнаю мову: %.0f с звуку…", pos)
    items = run_whisper(settings, wav, work / "whisper_output")

    out: dict[int, list[Word]] = {sh.index: [] for sh in shorts}
    for it in items:
        text = (it.get("text") or "").strip()
        off = it.get("offsets") or {}
        if not text or "from" not in off:
            continue
        t0, t1 = off["from"] / 1000.0, off["to"] / 1000.0
        mid = (t0 + t1) / 2
        for p0, src0, dur, idx in pieces:
            if p0 - 0.05 <= mid <= p0 + dur + 0.05:
                s = src0 + max(0.0, t0 - p0)
                e = src0 + min(dur, max(t1 - p0, t0 - p0 + 0.1))
                out[idx].append(Word(text, s, e))
                break
    return out


# ------------------------------------------------------------------ фрази

def _clean_words(words: list[Word], speech: SpeechMap | None, hallucinations: list[str]) -> list[Word]:
    out = []
    for w in words:
        if re.fullmatch(r"[\W_]+", w.text) and out:
            out[-1].text += w.text          # розділові знаки — до попереднього слова
            continue
        if w.text.startswith("[") or w.text.startswith("("):   # [музика], (сміх) тощо
            continue
        if speech is not None:
            n = max(1, int((w.end - w.start) / 0.05))
            hits = sum(speech.is_speech(w.start + k * 0.05) for k in range(n))
            if hits == 0:
                continue                     # у цей час ви мовчали — найімовірніше «галюцинація»
        out.append(w)
    text = " ".join(w.text for w in out).lower()
    for h in hallucinations:
        if h in text and len(text) < len(h) + 25:
            return []
    return out


def _wrap(text: str, max_line: int, max_lines: int) -> str:
    words = text.split()
    if len(text) <= max_line or max_lines < 2:
        return text
    best, best_cost = text, 1e9
    for i in range(1, len(words)):
        a, b = " ".join(words[:i]), " ".join(words[i:])
        cost = abs(len(a) - len(b)) + (100 if max(len(a), len(b)) > max_line else 0)
        if cost < best_cost:
            best, best_cost = f"{a}\n{b}", cost
    return best


def build_cues(sh: Short, words: list[Word], st: dict, speech: SpeechMap | None, hallucinations: list[str]) -> list[Cue]:
    words = _clean_words(sorted(words, key=lambda w: w.start), speech, hallucinations)
    max_chars = int(st["max_chars_per_line"]) * int(st["max_lines"])
    cues: list[Cue] = []
    cur: list[Word] = []
    cur_seg = None

    def seg_of(t):
        for i, s in enumerate(sh.segments):
            if s.start - 0.05 <= t <= s.end + 0.05:
                return i
        return None

    def flush():
        nonlocal cur
        if not cur:
            return
        a = sh.src_to_short(max(cur[0].start, sh.segments[seg_of(cur[0].start) or 0].start))
        b = sh.src_to_short(min(cur[-1].end, sh.segments[seg_of(cur[-1].start) or 0].end))
        if a is not None and b is not None and b > a:
            text = re.sub(r"\s+", " ", " ".join(w.text for w in cur)).strip()
            text = re.sub(r"[.,…]+$", "", text)
            if text:
                cues.append(Cue(a, b, text))
        cur = []

    for w in words:
        sg = seg_of(w.start)
        if sg is None:
            continue
        if cur:
            text_len = len(" ".join(x.text for x in cur)) + 1 + len(w.text)
            if (sg != cur_seg or w.start - cur[-1].end > st["pause_split_s"] or text_len > max_chars
                    or w.end - cur[0].start > st["max_cue_s"]):
                flush()
        cur.append(w)
        cur_seg = sg
        if re.search(r"[.!?]$", w.text):
            flush()
    flush()

    # мінімальна тривалість і без накладань
    for i, c in enumerate(cues):
        nxt = cues[i + 1].start if i + 1 < len(cues) else sh.duration
        if c.end - c.start < st["min_cue_s"]:
            c.end = min(nxt, c.start + st["min_cue_s"])
        c.end = min(c.end, nxt)
        c.text = _wrap(c.text, int(st["max_chars_per_line"]), int(st["max_lines"]))
    return [c for c in cues if c.end - c.start > 0.15]


def add_emoji(cues: list[Cue], sh: Short, st: dict) -> None:
    emo_map = st.get("emotion_emoji") or {}
    kw_map = st.get("keyword_emoji") or {}
    voice = [(sh.src_to_short(e.t), e.extra.get("kind")) for e in sh.events if e.type == E.VOICE]
    for c in cues:
        emoji = None
        for t, kind in voice:
            if t is not None and kind in emo_map and c.start - 0.5 <= t <= c.end + 0.5:
                emoji = emo_map[kind]
                break
        if not emoji:
            low = c.text.lower()
            for k, v in kw_map.items():
                if k.lower() in low:
                    emoji = v
                    break
        if emoji and emoji not in c.text:
            c.text = f"{c.text} {emoji}"


# ------------------------------------------------------------------ SRT

def srt_time(t: float) -> str:
    t = max(0.0, t)
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = int(t % 60)
    ms = int(round((t - int(t)) * 1000))
    if ms == 1000:
        s, ms = s + 1, 0
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def write_srt(cues: list[Cue], path: Path) -> Path:
    lines = []
    for i, c in enumerate(cues, 1):
        lines += [str(i), f"{srt_time(c.start)} --> {srt_time(c.end)}", c.text, ""]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def read_srt(path: Path) -> list[Cue]:
    """Читає SRT (наприклад, виправлений вручну) назад у фрази."""
    text = path.read_text(encoding="utf-8-sig").replace("\r\n", "\n")
    cues = []
    for block in re.split(r"\n\s*\n", text.strip()):
        rows = block.strip().split("\n")
        if len(rows) < 2:
            continue
        tl = rows[1] if "-->" in rows[1] else rows[0]
        m = re.match(r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)", tl.strip())
        if not m:
            continue
        g = [int(x) for x in m.groups()]
        a = g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 1000
        b = g[4] * 3600 + g[5] * 60 + g[6] + g[7] / 1000
        body = rows[rows.index(tl) + 1:]
        cues.append(Cue(a, b, "\n".join(body).strip()))
    return cues
