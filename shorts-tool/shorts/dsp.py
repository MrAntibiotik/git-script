"""Базова обробка звуку: гучність по кадрах, «звичайний рівень», відрізки, піки."""

from __future__ import annotations

import numpy as np


def frame_rms_db(x: np.ndarray, sr: int, hop_s: float = 0.02, win_s: float = 0.05) -> tuple[np.ndarray, float]:
    """Гучність (дБ) кадрами. Повертає (масив дБ, крок у секундах)."""
    hop = max(1, int(sr * hop_s))
    win = max(hop, int(sr * win_s))
    if len(x) < win:
        return np.full(1, -100.0, np.float32), hop / sr
    sq = np.concatenate([[0.0], np.cumsum(x.astype(np.float64) ** 2)])
    starts = np.arange(0, len(x) - win + 1, hop)
    energy = (sq[starts + win] - sq[starts]) / win
    db = 10 * np.log10(energy + 1e-10)
    return db.astype(np.float32), hop / sr


def rolling_baseline(db: np.ndarray, step_s: float, window_s: float, floor_db: float,
                     percentile: float = 50.0) -> np.ndarray:
    """«Звичайний рівень» для кожного кадру: медіана гучності звуків (вище порогу тиші)
    у ковзному вікні window_s. Рахується по секундах — швидко навіть для годин звуку."""
    per_sec = max(1, int(round(1.0 / step_s)))
    n_sec = int(np.ceil(len(db) / per_sec))
    pad = n_sec * per_sec - len(db)
    m = np.concatenate([db, np.full(pad, -100.0, np.float32)]).reshape(n_sec, per_sec)
    sec = np.full(n_sec, np.nan, np.float32)
    active = m > floor_db
    for i in range(n_sec):
        vals = m[i][active[i]]
        if vals.size >= max(3, per_sec // 5):
            sec[i] = np.percentile(vals, percentile)
    valid = ~np.isnan(sec)
    if not valid.any():
        return np.full(len(db), floor_db + 10, np.float32)
    # Медіана лише по секундах, де є звук (тиша не тягне рівень вниз,
    # а поодинокий гучний момент не «розмазується» на сусідні секунди).
    half = max(1, int(window_s) // 2)
    padded = np.concatenate([np.full(half, np.nan), sec, np.full(half, np.nan)])
    from numpy.lib.stride_tricks import sliding_window_view
    windows = sliding_window_view(padded, 2 * half + 1)
    with np.errstate(all="ignore"):
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            smooth = np.nanmedian(windows, axis=1)
    smooth = np.where(np.isnan(smooth), np.nanmedian(sec), smooth).astype(np.float32)
    return np.repeat(smooth, per_sec)[: len(db)]


def runs(mask: np.ndarray, max_gap: int = 0, min_len: int = 1) -> list[tuple[int, int]]:
    """Відрізки [початок, кінець) де mask=True; дірки до max_gap кадрів склеюються."""
    idx = np.flatnonzero(mask)
    if idx.size == 0:
        return []
    out = []
    s = p = idx[0]
    for i in idx[1:]:
        if i - p > max_gap + 1:
            out.append((s, p + 1))
            s = i
        p = i
    out.append((s, p + 1))
    return [(a, b) for a, b in out if b - a >= min_len]


def pick_peaks(score: np.ndarray, threshold: float, min_dist: int) -> list[int]:
    """Локальні максимуми вище порогу, не ближче min_dist один до одного (найсильніші — першими)."""
    cand = np.flatnonzero(score >= threshold)
    if cand.size == 0:
        return []
    order = cand[np.argsort(-score[cand])]
    taken: list[int] = []
    blocked = np.zeros(len(score), bool)
    for i in order:
        if blocked[i]:
            continue
        taken.append(int(i))
        blocked[max(0, i - min_dist):i + min_dist + 1] = True
    return sorted(taken)
