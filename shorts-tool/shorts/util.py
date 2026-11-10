"""Дрібні допоміжні функції: час, нечітке порівняння тексту."""

from __future__ import annotations

import re
from difflib import SequenceMatcher


# ---------------------------------------------------------------- час

def fmt_time(seconds: float | None, decimals: int = 1) -> str:
    """123.4 -> '2:03.4', 3725 -> '1:02:05.0'."""
    if seconds is None:
        return "—"
    neg = seconds < 0
    s = abs(seconds)
    h = int(s // 3600)
    m = int((s % 3600) // 60)
    sec = s - h * 3600 - m * 60
    width = 3 + decimals if decimals else 2
    sec_str = f"{sec:0{width}.{decimals}f}" if decimals else f"{int(sec):02d}"
    out = f"{h}:{m:02d}:{sec_str}" if h else f"{m}:{sec_str}"
    return ("-" if neg else "") + out


def parse_time(value: str | float | int | None) -> float | None:
    """'2:03.5' / '1:02:05' / '95' / 95 -> секунди. Порожнє -> None."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", ".")
    if not text:
        return None
    parts = text.split(":")
    try:
        nums = [float(p) for p in parts]
    except ValueError:
        raise ValueError(f"Не вдалося розібрати час «{value}». Приклади: 95, 1:35, 1:02:05")
    total = 0.0
    for n in nums:
        total = total * 60 + n
    return total


# ---------------------------------------------------------------- текст

# Латинські літери, схожі на кириличні (OCR часто їх плутає).
_LOOKALIKE = str.maketrans({
    "a": "а", "c": "с", "e": "е", "i": "і", "o": "о", "p": "р", "x": "х",
    "y": "у", "k": "к", "m": "м", "h": "н", "t": "т", "b": "в", "r": "г",
    "n": "п", "u": "и", "ё": "е", "ы": "и", "э": "е", "ъ": "ь",
})


def normalize_words(text: str) -> str:
    """Нижній регістр, латиниця->кирилиця, лише літери й пробіли."""
    t = (text or "").lower().translate(_LOOKALIKE)
    t = re.sub(r"[^a-zа-яіїєґ'’ ]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def ratio(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


def partial_ratio(needle: str, hay: str) -> float:
    """Найкраща схожість needle з будь-яким фрагментом hay такої ж довжини."""
    if not needle or not hay:
        return 0.0
    if len(hay) <= len(needle):
        return ratio(needle, hay)
    n = len(needle)
    best = 0.0
    for i in range(0, len(hay) - n + 1):
        r = ratio(needle, hay[i:i + n])
        if r > best:
            best = r
            if best >= 0.999:
                break
    return best


def contains_keyword(text: str, keywords: list[str], min_ratio: float = 0.8) -> str | None:
    """Повертає перше ключове слово, яке (нечітко) є в тексті, або None."""
    hay = normalize_words(text)
    if not hay:
        return None
    for kw in keywords:
        needle = normalize_words(kw)
        if needle and partial_ratio(needle, hay) >= min_ratio:
            return kw
    return None


_NUM_RE = re.compile(r"\d{1,3}(?:[  .,]\d{3})+|\d+")


def first_number(text: str) -> int | None:
    """'1 620' -> 1620, 'x 612 Знищив' -> 612."""
    if not text:
        return None
    m = _NUM_RE.search(text)
    if not m:
        return None
    digits = re.sub(r"\D", "", m.group(0))
    return int(digits) if digits else None
