"""Мій голос: крик, сміх, різкий сплеск гучності відносно мого звичайного рівня.

«Звичайний рівень» рахується автоматично: медіана гучності вашого голосу за останню
хвилину (ковзне вікно), тому тихі й гучні ділянки стріму оцінюються кожна відносно себе.

  сплеск — гучність вища за звичайну на spike_db (10 дБ) хоча б 0,15 с;
  крик   — сплеск ≥ scream_min_db, тривалістю ≥ 0,3 с і з високим тоном (≥ scream_min_hz);
  сміх   — серія 3+ коротких ритмічних поштовхів голосу (3–8 на секунду) протягом 1–3 с.

Працює по виділеній доріжці голосу. Якщо звук не розділено (мікс), пороги вищі
(mix_spike_db), бо гучні звуки гри теж потрапляють у мікс.
Звуки екіпажу, знайдені детектором звуків гри на доріжці голосу, з емоцій виключаються.
"""

from __future__ import annotations

import logging

import numpy as np

from . import events as E
from .audio import AudioChunk, load_with_pad
from .dsp import frame_rms_db, rolling_baseline, runs
from .events import Event

log = logging.getLogger("voice")

KIND_PRIORITY = {"крик": 3, "сміх": 2, "сплеск": 1}


class VoiceDetector:
    def __init__(self, profile: dict):
        self.profile = profile
        self.cfg = profile["voice"]

    def detect(self, chunks: list[AudioChunk], exclude: list[tuple[float, float]] | None = None,
               progress=None) -> list[Event]:
        if not self.profile["signals"].get("voice"):
            return []
        out: list[Event] = []
        for i, ch in enumerate(chunks):
            out += self._chunk(chunks, i)
            if progress:
                progress((i + 1) / len(chunks))
        if exclude:
            out = [e for e in out if not any(a - 0.3 <= e.t <= b + 0.3 for a, b in exclude)]
        return self._merge(out)

    def _chunk(self, chunks: list[AudioChunk], i: int) -> list[Event]:
        c = self.cfg
        ch = chunks[i]
        y, sr, t0 = load_with_pad(chunks, i, "voice", pad_s=c["baseline_window_s"] / 2)
        if y.size < sr:
            return []
        db, step = frame_rms_db(y, sr, hop_s=0.02, win_s=0.05)
        floor = max(c["floor_db"], float(np.percentile(db, 20)) + 6)   # тиша / шум мікрофона
        base = rolling_baseline(db, step, c["baseline_window_s"], floor)
        exc = db - base
        spike_db = c["spike_db"] if ch.separated else c["mix_spike_db"]
        extra_db = 0 if ch.separated else c["mix_spike_db"] - c["spike_db"]
        out: list[Event] = []

        # --- сплески й крики
        min_len = max(1, int(c["min_duration_s"] / step))
        for a, b in runs(exc >= spike_db, max_gap=3, min_len=min_len):
            t = t0 + a * step
            if t < ch.c0 or t >= ch.c1:
                continue
            dur = (b - a) * step
            peak = float(exc[a:b].max())
            kind = "сплеск"
            f0 = None
            if dur >= c["scream_min_duration_s"] and peak >= c["scream_min_db"] + extra_db:
                f0 = self._median_f0(y, sr, a, b, step)
                if f0 and f0 >= c["scream_min_hz"]:
                    kind = "крик"
            intensity = min(2.0, (peak - spike_db) / 10 + (0.5 if kind == "крик" else 0) + 0.5)
            detail = f"{kind}: +{peak:.0f} дБ до звичайного, {dur:.1f} с" + (f", тон {f0:.0f} Гц" if f0 else "")
            out.append(Event(round(t, 3), E.VOICE, "голос", value=round(peak, 1),
                             confidence=round(min(1.0, 0.5 + (peak - spike_db) / 12), 2), detail=detail,
                             extra={"kind": kind, "intensity": round(intensity, 2), "duration": round(dur, 2)}))

        # --- сміх
        if c["laugh"]["enabled"]:
            out += self._laughs(y, sr, t0, db, exc, step, ch, extra_db)
        return out

    def _median_f0(self, y: np.ndarray, sr: int, a: int, b: int, step: float) -> float | None:
        import librosa
        s0 = int(a * step * sr)
        s1 = int(b * step * sr) + int(0.05 * sr)
        seg = y[s0:s1]
        if len(seg) < 2048:
            return None
        try:
            f0 = librosa.yin(seg, fmin=70, fmax=1000, sr=sr, frame_length=2048, hop_length=320)
        except Exception:
            return None
        f0 = f0[(f0 > 75) & (f0 < 990)]
        return float(np.median(f0)) if f0.size >= 3 else None

    def _laughs(self, y, sr, t0, db, exc, step, ch, extra_db) -> list[Event]:
        """Сміх = серія рівномірних поштовхів голосу 3–8 разів на секунду, гучніше за звичайну
        мову і з вищим тоном. (Звичайна мова теж має ритм складів, тому перевіряємо все разом.)"""
        from scipy.signal import find_peaks
        lc = self.cfg["laugh"]
        out: list[Event] = []
        win = int(lc["window_s"] / step)
        hop = max(1, int(0.25 / step))
        lag_min, lag_max = int(0.12 / step), int(0.35 / step)
        env = np.power(10, db / 20)
        active = exc >= lc["min_db"] + extra_db
        last_t = -1e9
        for s in range(0, max(0, len(db) - win), hop):
            if active[s:s + win].mean() < 0.35:
                continue
            e = env[s:s + win]
            seg = e - e.mean()
            denom = float((seg ** 2).sum())
            if denom <= 1e-12:
                continue
            ac = np.correlate(seg, seg, mode="full")[win - 1:] / denom
            if lag_max + 1 >= len(ac):
                continue
            pl = lag_min + int(np.argmax(ac[lag_min:lag_max]))
            # справжній ритм — це локальний пік автокореляції, а не край діапазону
            if pl <= lag_min or not (ac[pl] > ac[pl - 1] and ac[pl] >= ac[pl + 1]):
                continue
            if ac[pl] < lc["min_periodicity"]:
                continue
            peaks, _ = find_peaks(e, height=np.median(e) * 1.3, distance=max(1, int(0.1 / step)))
            if len(peaks) < lc["min_bursts"]:
                continue
            gaps = np.diff(peaks)
            if gaps.size >= 2 and gaps.std() / max(gaps.mean(), 1e-6) > lc["max_irregularity"]:
                continue
            t = t0 + (s + int(peaks[0])) * step   # час першого поштовху
            if t < ch.c0 or t >= ch.c1 or t - last_t < lc["window_s"]:
                continue
            f0 = self._median_f0(y, sr, s, s + win, step)
            if f0 is None or f0 < lc["min_hz"]:
                continue
            last_t = t
            per = pl * step
            out.append(Event(round(t, 3), E.VOICE, "голос", value=round(float(exc[s:s + win].max()), 1),
                             confidence=round(float(min(1.0, ac[pl])), 2),
                             detail=f"сміх: {len(peaks)} поштовхів, ритм {1 / per:.1f}/с, тон {f0:.0f} Гц",
                             extra={"kind": "сміх", "intensity": round(float(min(2.0, 0.6 + ac[pl])), 2),
                                    "duration": round(lc["window_s"], 2)}))
        return out

    def _merge(self, evs: list[Event]) -> list[Event]:
        evs.sort(key=lambda e: e.t)
        out: list[Event] = []
        for e in evs:
            if out and e.t - out[-1].t <= self.cfg["merge_s"]:
                o = out[-1]
                if KIND_PRIORITY[e.extra["kind"]] > KIND_PRIORITY[o.extra["kind"]] or (
                        e.extra["kind"] == o.extra["kind"] and (e.value or 0) > (o.value or 0)):
                    o.detail, o.extra["kind"], o.value = e.detail, e.extra["kind"], e.value
                o.extra["intensity"] = max(o.extra["intensity"], e.extra["intensity"])
                o.confidence = max(o.confidence, e.confidence)
                continue
            out.append(e)
        return out


def describe(evs: list[Event]) -> str:
    by: dict[str, int] = {}
    for e in evs:
        by[e.extra.get("kind", "?")] = by.get(e.extra.get("kind", "?"), 0) + 1
    return ", ".join(f"{k}: {v}" for k, v in sorted(by.items())) or "немає"


def count_loud_spikes(chunks: list[AudioChunk], spike_db: float = 10.0) -> int:
    """Скільки «сплесків гучності» в міксі (для порівняння: звук сам дає багато зайвого)."""
    n = 0
    for i, ch in enumerate(chunks):
        y, sr, t0 = load_with_pad(chunks, i, "mix", pad_s=0)
        db, step = frame_rms_db(y, sr)
        base = rolling_baseline(db, step, 30, -60)
        n += len(runs(db - base >= spike_db, max_gap=5, min_len=3))
    return n
