"""Звуки гри: пошук зразків із бібліотеки samples/<гра>/<категорія>/*.wav.

Як працює: і зразок, і звук гри перетворюються на «спектрограму» (як звук розподілений
за частотами в часі). Потім зразок «прикладається» до кожного моменту запису, і
рахується схожість від 0 до 1 (нормована кореляція). Де схожість вища за поріг —
там цей звук.

Категорія (назва папки) задає тип події в профілі гри (game_audio.categories), напр.:
  samples/wot/пробиття/*.wav  -> влучання
  samples/wot/знищено/*.wav   -> знищення ворога
Нові зразки зручно вирізати через make_sample.bat.

Якщо звук не розділено (немає Demucs або вимкнено), пошук іде по міксу,
а поріг множиться на mix_threshold_factor (знижується).

Додатково (для ігор без зорових сигналів, напр. PUBG): «гучні звуки гри» —
різкий сплеск гучності звуку гри відносно звичайного рівня (loudness_spikes).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from . import events as E
from .audio import SR_GAME, AudioChunk, load_with_pad
from .config import ROOT
from .dsp import frame_rms_db, pick_peaks, rolling_baseline, runs
from .events import Event
from .util import fmt_time

log = logging.getLogger("game_audio")

N_FFT = 1024
HOP = 256
N_MELS = 64
AUDIO_EXT = {".wav", ".flac", ".ogg"}


def samples_root(profile: dict) -> Path:
    return ROOT / "samples" / profile["id"]


def mel_db(x: np.ndarray, sr: int = SR_GAME) -> np.ndarray:
    import librosa
    m = librosa.feature.melspectrogram(y=x.astype(np.float32), sr=sr, n_fft=N_FFT, hop_length=HOP,
                                       n_mels=N_MELS, fmin=60, fmax=min(11000, sr // 2), power=2.0)
    return librosa.power_to_db(m, ref=1.0, top_db=None).clip(-90, None).astype(np.float32)


@dataclass
class Template:
    category: str
    name: str
    mel: np.ndarray          # (N_MELS, L)
    centered: np.ndarray     # mel - mean
    norm: float
    mean_db: float
    duration: float


def load_templates(profile: dict) -> list[Template]:
    import librosa
    root = samples_root(profile)
    cats = profile["game_audio"]["categories"]
    for c in cats:
        (root / c).mkdir(parents=True, exist_ok=True)
    out: list[Template] = []
    if not root.exists():
        return out
    for f in sorted(root.rglob("*")):
        if f.suffix.lower() not in AUDIO_EXT or not f.is_file():
            continue
        cat = f.parent.name if f.parent != root else "інше"
        try:
            y, _ = librosa.load(str(f), sr=SR_GAME, mono=True)
        except Exception as e:
            log.warning("Не вдалося прочитати зразок %s: %s (потрібен WAV/FLAC/OGG)", f.name, e)
            continue
        y, _ = librosa.effects.trim(y, top_db=40)
        y = y[: int(3.0 * SR_GAME)]
        if len(y) < int(0.12 * SR_GAME):
            log.warning("Зразок %s закороткий (< 0,12 с) — пропускаю.", f.name)
            continue
        m = mel_db(y)
        c = m - m.mean()
        norm = float(np.sqrt((c ** 2).sum()))
        if norm < 1e-3:
            continue
        out.append(Template(cat, f.name, m, c, norm, float(m.mean()), len(y) / SR_GAME))
    return out


def match_template(S: np.ndarray, tpl: Template) -> tuple[np.ndarray, np.ndarray]:
    """Нормована кореляція зразка з кожною позицією спектрограми S.
    Повертає (схожість 0..1, середня гучність вікна в дБ) довжиною N-L+1."""
    from scipy.signal import fftconvolve
    B, L = tpl.mel.shape
    N = S.shape[1]
    if N < L:
        return np.zeros(0, np.float32), np.zeros(0, np.float32)
    num = fftconvolve(S, tpl.centered[:, ::-1], mode="valid", axes=1).sum(axis=0)
    cs = np.concatenate([[0.0], np.cumsum(S.sum(axis=0, dtype=np.float64))])
    cs2 = np.concatenate([[0.0], np.cumsum((S.astype(np.float64) ** 2).sum(axis=0))])
    w1 = cs[L:] - cs[:-L]
    w2 = cs2[L:] - cs2[:-L]
    var = np.maximum(w2 - w1 ** 2 / (B * L), 1e-6)
    ncc = num[: len(w1)] / (tpl.norm * np.sqrt(var))
    return ncc.astype(np.float32), (w1 / (B * L)).astype(np.float32)


class GameSoundDetector:
    def __init__(self, settings: dict, profile: dict):
        self.profile = profile
        self.cfg = profile["game_audio"]
        self.templates = load_templates(profile) if profile["signals"].get("game_audio") else []
        root = samples_root(profile)
        if profile["signals"].get("game_audio"):
            if self.templates:
                by_cat: dict[str, int] = {}
                for t in self.templates:
                    by_cat[t.category] = by_cat.get(t.category, 0) + 1
                log.info("Зразки звуків (%s): %s", root,
                         ", ".join(f"{c} — {n}" for c, n in sorted(by_cat.items())))
            else:
                log.info("У %s ще немає зразків звуків — пошук зразків пропущено. "
                         "Додайте їх через make_sample.bat (README, етап 2).", root)

    def _cat_cfg(self, cat: str) -> dict:
        d = {"type": E.GAME_SOUND, "weight": 0.5, "threshold": None, "stems": None}
        d.update(self.cfg["categories"].get(cat, {}))
        return d

    def detect(self, chunks: list[AudioChunk], progress=None) -> list[Event]:
        out: list[Event] = []
        if not self.profile["signals"].get("game_audio"):
            return out
        spikes_cfg = self.cfg.get("loudness_spikes", {})
        for i, ch in enumerate(chunks):
            stems_needed: set[str] = set()
            for t in self.templates:
                stems_needed.update(self._cat_cfg(t.category)["stems"] or self.cfg["stems"])
            if not ch.separated:
                stems_needed = {"mix"} if stems_needed else set()   # усе одно все — мікс
            mels: dict[str, tuple[np.ndarray, float]] = {}
            for stem in stems_needed:
                y, sr, t0 = load_with_pad(chunks, i, stem, pad_s=3.0)
                if stem == "voice":  # голос збережено в 16 кГц — для спектрограми переводимо в 22 кГц
                    from .audio import resample
                    y = resample(y, sr, SR_GAME)
                mels[stem] = (mel_db(y), t0)
            for tpl in self.templates:
                out += self._match(tpl, mels, ch)
            if spikes_cfg.get("enabled"):
                out += self._loud_spikes(chunks, i, spikes_cfg)
            if progress:
                progress((i + 1) / len(chunks))
        return self._dedupe(out)

    def _match(self, tpl: Template, mels: dict, ch: AudioChunk) -> list[Event]:
        cc = self._cat_cfg(tpl.category)
        thr = cc["threshold"] or self.cfg["threshold"]
        if not ch.separated:
            thr *= self.cfg["mix_threshold_factor"]
        best: dict[int, tuple[float, str, float]] = {}
        frame_s = HOP / SR_GAME
        for stem, (S, t0) in mels.items():
            ncc, level = match_template(S, tpl)
            if ncc.size == 0:
                continue
            ncc = np.where(level >= tpl.mean_db - self.cfg["level_tolerance_db"], ncc, 0)
            L = tpl.mel.shape[1]
            min_dist = max(L, int(self.cfg["min_gap_s"] / frame_s))
            for p in pick_peaks(ncc, thr, min_dist):
                t = t0 + p * frame_s
                if t < ch.c0 or t >= ch.c1:
                    continue
                key = int(round(t / 0.25))
                if key not in best or ncc[p] > best[key][0]:
                    best[key] = (float(ncc[p]), stem, t)
        evs = []
        for score, stem, t in best.values():
            # Тип завжди GAME_SOUND; що означає звук (влучання/знищення…) — у extra["means"].
            evs.append(Event(round(t, 3), E.GAME_SOUND, f"звук:{tpl.category}", value=None,
                             confidence=round(score, 3),
                             detail=f"{tpl.name} ({stem}, схожість {score:.2f})",
                             extra={"category": tpl.category, "means": cc["type"], "weight": cc["weight"],
                                    "stem": stem,
                                    "duration": round(tpl.duration, 2), "audio": True}))
        return evs

    def _loud_spikes(self, chunks: list[AudioChunk], i: int, cfg: dict) -> list[Event]:
        ch = chunks[i]
        stem = "game" if ch.separated else "mix"
        y, sr, t0 = load_with_pad(chunks, i, stem, pad_s=30.0)
        db, step = frame_rms_db(y, sr)
        base = rolling_baseline(db, step, cfg["baseline_window_s"], cfg["floor_db"])
        spike = cfg["spike_db"] if ch.separated else cfg["spike_db"] + cfg.get("mix_extra_db", 3)
        exc = db - base
        out = []
        for a, b in runs(exc >= spike, max_gap=2, min_len=max(1, int(cfg["min_duration_s"] / step))):
            t = t0 + a * step
            if t < ch.c0 or t >= ch.c1:
                continue
            peak = float(exc[a:b].max())
            out.append(Event(round(t, 3), E.GAME_SOUND, "звук:гучність", value=round(peak, 1),
                             confidence=min(1.0, peak / (spike * 2)),
                             detail=f"сплеск звуку гри +{peak:.0f} дБ",
                             extra={"category": "гучність", "means": E.GAME_SOUND, "weight": cfg["weight"],
                                    "stem": stem,
                                    "duration": round((b - a) * step, 2), "audio": True}))
        return out

    @staticmethod
    def _dedupe(evs: list[Event]) -> list[Event]:
        """Одна подія на категорію в межах 0,5 с (з різних зразків тієї ж категорії)."""
        evs.sort(key=lambda e: e.t)
        out: list[Event] = []
        for e in evs:
            dup = next((o for o in reversed(out[-10:]) if o.extra.get("category") == e.extra.get("category")
                        and abs(o.t - e.t) <= 0.5), None)
            if dup:
                if e.confidence > dup.confidence:
                    dup.t, dup.confidence, dup.detail = e.t, e.confidence, e.detail
                continue
            out.append(e)
        return out


def describe(evs: list[Event]) -> str:
    by: dict[str, int] = {}
    for e in evs:
        by[e.extra.get("category", "?")] = by.get(e.extra.get("category", "?"), 0) + 1
    return ", ".join(f"{k}: {v}" for k, v in sorted(by.items())) or "немає"


__all__ = ["GameSoundDetector", "load_templates", "match_template", "mel_db", "samples_root",
           "describe", "fmt_time"]
