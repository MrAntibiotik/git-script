"""Підготовка звуку: витяг із запису, розділення на «голос» і «гру», кеш частинами.

Режими (config/settings.json → recording.mode):
  mixed    — одна доріжка (гра + мікрофон разом). Голос відокремлюється нейромережею
             Demucs на відеокарті. Якщо розділення вимкнене або не запустилось,
             «голос» і «гра» = той самий мікс (детектори знижують пороги).
  separate — окремі доріжки гри й мікрофона в записі (OBS). Розділення не потрібне.

Для кожної 10-хвилинної частини в work/ зберігаються три файли FLAC:
  voice (16 кГц моно — для детектора емоцій і, пізніше, субтитрів),
  game  (22,05 кГц моно — для пошуку звуків гри),
  mix   (22,05 кГц моно — оригінальний мікс, запасний варіант для пошуку зразків).
Оригінальний запис не змінюється.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .config import models_dir, work_dir
from .errors import UserError
from .media import VideoInfo, find_tool, run
from .util import fmt_time

log = logging.getLogger("audio")

SR_SEP = 44100     # частота для Demucs
SR_VOICE = 16000   # голос (whisper теж хоче 16 кГц)
SR_GAME = 22050    # звуки гри


@dataclass
class AudioChunk:
    c0: float
    c1: float
    voice: Path
    game: Path
    mix: Path
    separated: bool

    def load(self, stem: str) -> tuple[np.ndarray, int]:
        import soundfile as sf
        path = {"voice": self.voice, "game": self.game, "mix": self.mix}[stem]
        data, sr = sf.read(str(path), dtype="float32", always_2d=False)
        return data, sr


# ------------------------------------------------------------------ ffmpeg -> numpy

def read_audio(settings: dict, path: Path, start: float, duration: float, sr: int, channels: int,
               track: int = 0, filter_complex: str | None = None) -> np.ndarray:
    """Повертає float32 масив (samples, channels) або (samples,) для моно."""
    ffmpeg = find_tool(settings, "ffmpeg")
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
           "-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", str(path), "-vn", "-sn", "-dn"]
    if filter_complex:
        cmd += ["-filter_complex", filter_complex, "-map", "[a]"]
    else:
        cmd += ["-map", f"0:a:{track}"]
    cmd += ["-ac", str(channels), "-ar", str(sr), "-f", "f32le", "pipe:1"]
    res = run(cmd)
    a = np.frombuffer(res.stdout, dtype=np.float32)
    if channels > 1:
        a = a[: len(a) - len(a) % channels].reshape(-1, channels)
    return a.copy()


def resample(x: np.ndarray, sr_from: int, sr_to: int) -> np.ndarray:
    if sr_from == sr_to:
        return x.astype(np.float32)
    from scipy.signal import resample_poly
    g = math.gcd(sr_from, sr_to)
    return resample_poly(x, sr_to // g, sr_from // g).astype(np.float32)


# ------------------------------------------------------------------ Demucs

class Separator:
    """Нейромережа Demucs (htdemucs): відділяє голос (vocals) від решти звуку."""

    def __init__(self, settings: dict):
        cfg = settings["separation"]
        mdir = models_dir(settings)
        os.environ.setdefault("HF_HOME", str(mdir / "huggingface"))
        os.environ.setdefault("TORCH_HOME", str(mdir / "torch"))
        try:
            import torch
            from demucs.apply import apply_model
            from demucs.pretrained import get_model
        except ImportError as e:
            raise UserError(f"Не встановлено Demucs/PyTorch ({e}).",
                            "Запустіть install.bat ще раз (етап 2 додав нові бібліотеки).")
        self.torch = torch
        self.apply_model = apply_model
        want = cfg.get("device", "cuda")
        if want == "cuda" and not torch.cuda.is_available():
            log.warning("CUDA недоступна — розділення звуку піде на процесорі (у 10–30 разів повільніше).")
            want = "cpu"
        self.device = want
        log.info("Завантажую модель розділення звуку %s (%s)… (перший раз ~80 МБ з інтернету)",
                 cfg["model"], self.device.upper())
        try:
            self.model = get_model(cfg["model"])
        except Exception as e:
            raise UserError(f"Не вдалося завантажити модель Demucs «{cfg['model']}»: {e}",
                            "Перший запуск потребує інтернету. Перевірте з'єднання й спробуйте знову.")
        self.model.eval()
        self.vocals_idx = list(self.model.sources).index("vocals")
        self.shifts = int(cfg.get("shifts", 0))
        self.overlap = float(cfg.get("overlap", 0.25))

    def separate(self, stereo: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """stereo (n, 2) 44.1 кГц -> (голос моно, гра моно), 44.1 кГц."""
        th = self.torch
        wav = th.from_numpy(np.ascontiguousarray(stereo.T))  # (2, n)
        ref = wav.mean(0)
        mean, std = ref.mean(), ref.std() + 1e-8
        with th.no_grad():
            out = self.apply_model(self.model, ((wav - mean) / std)[None], shifts=self.shifts,
                                   split=True, overlap=self.overlap, progress=False, device=self.device)[0]
        out = out * std + mean
        vocals = out[self.vocals_idx].mean(0).cpu().numpy()
        mix_mono = stereo.mean(axis=1)
        game = mix_mono - vocals
        if self.device == "cuda":
            th.cuda.empty_cache()
        return vocals.astype(np.float32), game.astype(np.float32)


# ------------------------------------------------------------------ підготовка частин

class AudioPrep:
    def __init__(self, settings: dict, profile: dict, info: VideoInfo, force_mix: bool = False):
        self.settings = settings
        self.profile = profile
        self.info = info
        self.mode = settings["recording"]["mode"]
        if self.mode not in ("mixed", "separate"):
            raise UserError(f"Невідомий режим запису «{self.mode}» у settings.json.",
                            'Має бути "mixed" або "separate".')
        self.use_sep = (self.mode == "mixed" and settings["separation"]["enabled"] and not force_mix)
        self.separator: Separator | None = None
        self.sep_failed = False
        self._check_tracks()

    def _check_tracks(self):
        n = self.info.audio_streams
        if n == 0:
            raise UserError(f"У записі {self.info.path.name} немає звуку.", "Перевірте налаштування запису OBS.")
        rec = self.settings["recording"]
        need = ([rec["separate"]["game_audio_track"], rec["separate"]["mic_audio_track"]]
                if self.mode == "separate" else [rec["mixed"]["audio_track"]])
        if max(need) >= n:
            raise UserError(
                f"У записі {n} аудіодоріжок, а в settings.json вказано доріжку №{max(need)} (рахуються з 0).",
                "Для одного міксу: recording.mode = \"mixed\", audio_track = 0. Для окремих доріжок "
                "перевірте номери в recording.separate.")

    def _cache_dir(self) -> Path:
        rec = self.settings["recording"]
        key = json.dumps({"mode": self.mode, "rec": rec[self.mode], "sep": self.use_sep,
                          "model": self.settings["separation"]["model"], "v": 1}, sort_keys=True)
        h = hashlib.md5(key.encode()).hexdigest()[:8]
        st = self.info.path.stat()
        vid = hashlib.md5(f"{self.info.path.name}|{st.st_size}".encode()).hexdigest()[:6]
        safe = re.sub(r"[^\w\-]+", "_", self.info.path.stem)[:60]
        d = work_dir(self.settings) / f"{safe}_{vid}" / f"audio_{h}"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _ensure_separator(self) -> bool:
        if not self.use_sep or self.sep_failed:
            return False
        if self.separator is None:
            try:
                self.separator = Separator(self.settings)
            except UserError as e:
                log.warning("[УВАГА] Розділення звуку недоступне: %s\n"
                            "        Працюю по міксу зі зниженими порогами.", e)
                self.sep_failed = True
                return False
        return True

    def _make_chunk(self, c0: float, c1: float, d: Path) -> AudioChunk:
        import soundfile as sf
        base = d / f"chunk_{c0:09.1f}_{c1:09.1f}"
        paths = {s: base.with_name(base.name + f"_{s}.flac") for s in ("voice", "game", "mix")}
        meta = base.with_name(base.name + ".json")
        if meta.exists() and all(p.exists() for p in paths.values()):
            sep = json.loads(meta.read_text(encoding="utf-8")).get("separated", False)
            return AudioChunk(c0, c1, paths["voice"], paths["game"], paths["mix"], sep)

        rec = self.settings["recording"]
        dur = c1 - c0
        separated = False
        if self.mode == "separate":
            g, m = rec["separate"]["game_audio_track"], rec["separate"]["mic_audio_track"]
            game = read_audio(self.settings, self.info.path, c0, dur, SR_GAME, 1, track=g)
            voice = read_audio(self.settings, self.info.path, c0, dur, SR_VOICE, 1, track=m)
            mix = read_audio(self.settings, self.info.path, c0, dur, SR_GAME, 1,
                             filter_complex=f"[0:a:{g}][0:a:{m}]amix=inputs=2:normalize=0[a]")
            separated = True
        else:
            track = rec["mixed"]["audio_track"]
            if self._ensure_separator():
                stereo = read_audio(self.settings, self.info.path, c0, dur, SR_SEP, 2, track=track)
                try:
                    v44, g44 = self.separator.separate(stereo)
                    voice = resample(v44, SR_SEP, SR_VOICE)
                    game = resample(g44, SR_SEP, SR_GAME)
                    mix = resample(stereo.mean(axis=1), SR_SEP, SR_GAME)
                    separated = True
                except Exception as e:  # нестача відеопам'яті тощо
                    log.warning("[УВАГА] Розділення звуку не вдалося (%s). Далі працюю по міксу.", e)
                    self.sep_failed = True
            if not separated:
                mix = read_audio(self.settings, self.info.path, c0, dur, SR_GAME, 1, track=track)
                game = mix
                voice = read_audio(self.settings, self.info.path, c0, dur, SR_VOICE, 1, track=track)

        for name, data, sr in (("voice", voice, SR_VOICE), ("game", game, SR_GAME), ("mix", mix, SR_GAME)):
            tmp = paths[name].with_suffix(".tmp.flac")
            sf.write(str(tmp), np.clip(data, -1, 1), sr, subtype="PCM_16", format="FLAC")
            tmp.replace(paths[name])
        meta.write_text(json.dumps({"separated": separated}), encoding="utf-8")
        return AudioChunk(c0, c1, paths["voice"], paths["game"], paths["mix"], separated)

    def prepare(self, start: float = 0.0, end: float | None = None, progress=None) -> list[AudioChunk]:
        end = self.info.duration if end is None else min(end, self.info.duration)
        chunk = max(60.0, float(self.settings["processing"]["chunk_minutes"]) * 60)
        d = self._cache_dir()
        k0, k1 = int(start // chunk), int(math.ceil(end / chunk))
        out: list[AudioChunk] = []
        t_all = time.time()
        for i, k in enumerate(range(k0, k1)):
            c0, c1 = max(start, k * chunk), min(end, (k + 1) * chunk)
            if c1 - c0 < 0.5:
                continue
            t0 = time.time()
            ch = self._make_chunk(c0, c1, d)
            out.append(ch)
            dt = time.time() - t0
            log.info("  звук %d/%d (%s–%s): %s, %.0f с", i + 1, k1 - k0, fmt_time(c0, 0), fmt_time(c1, 0),
                     "розділено" if ch.separated else "мікс", dt)
            if progress:
                progress((c1 - start) / max(1e-6, end - start))
        if self.separator is not None:
            del self.separator
            self.separator = None
        log.info("Звук підготовлено за %.0f с. Кеш: %s", time.time() - t_all, d)
        return out


def load_with_pad(chunks: list[AudioChunk], i: int, stem: str, pad_s: float) -> tuple[np.ndarray, int, float]:
    """Звук частини i + хвіст попередньої частини (щоб не губити події на стику).
    Повертає (сигнал, частота, час_початку_сигналу)."""
    data, sr = chunks[i].load(stem)
    t0 = chunks[i].c0
    if i > 0 and pad_s > 0 and abs(chunks[i - 1].c1 - chunks[i].c0) < 0.01:
        prev, _ = chunks[i - 1].load(stem)
        n = int(pad_s * sr)
        data = np.concatenate([prev[-n:], data])
        t0 -= min(n, len(prev)) / sr
    return data, sr, t0
