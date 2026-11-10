"""Картинка з текстом для вшивання в MP4: великий білий шрифт, чорний контур, тінь, кольорові емодзі.

Рендеримо через Pillow (а не через libass у ffmpeg), бо так працюють кольорові емодзі
(шрифт Segoe UI Emoji у Windows) і не треба боротися з екрануванням шляхів у ffmpeg.
"""

from __future__ import annotations

import logging
import re
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageColor, ImageDraw, ImageFilter, ImageFont

log = logging.getLogger("textimg")

_EMOJI_RE = re.compile(
    "([\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\U0001F1E6-\U0001F1FF]"
    "(?:[\uFE0F\u200D\U0001F3FB-\U0001F3FF]|[\U0001F000-\U0001FAFF\u2600-\u27BF])*)")

TEXT_FALLBACKS = ["C:/Windows/Fonts/arialbd.ttf", "C:/Windows/Fonts/segoeuib.ttf",
                  "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "DejaVuSans-Bold.ttf", "arialbd.ttf"]
EMOJI_FALLBACKS = ["C:/Windows/Fonts/seguiemj.ttf", "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf"]
_warned: set[str] = set()


@lru_cache(maxsize=32)
def _font(path: str, size: int):
    return ImageFont.truetype(path, size)


def load_font(preferred: str, size: int, fallbacks: list[str]):
    for p in [preferred] + fallbacks:
        if not p:
            continue
        try:
            return _font(p, size)
        except OSError:
            continue
    if "text" not in _warned:
        log.warning("Не знайдено шрифт %s — використовую стандартний (може бути без кирилиці).", preferred)
        _warned.add("text")
    return ImageFont.load_default()


def split_runs(line: str) -> list[tuple[str, bool]]:
    """Рядок -> [(фрагмент, це_емодзі)]."""
    out = []
    pos = 0
    for m in _EMOJI_RE.finditer(line):
        if m.start() > pos:
            out.append((line[pos:m.start()], False))
        out.append((m.group(0), True))
        pos = m.end()
    if pos < len(line):
        out.append((line[pos:], False))
    return out


def _emoji_font(style: dict, size: int):
    for p in [style.get("emoji_font")] + EMOJI_FALLBACKS:
        if not p or not Path(p).exists():
            continue
        try:
            return _font(p, size)
        except OSError:
            # Noto Color Emoji має лише розмір 109 — масштабуємо окремо
            try:
                return _font(p, 109)
            except OSError:
                continue
    return None


def render_text(text: str, style: dict, font_size: int | None = None, max_width: int = 1000) -> Image.Image:
    """Повертає RGBA-картинку з текстом (рядки через \\n), обрізану по вмісту з полями."""
    size = int(font_size or style.get("font_size", 66))
    lines = [l for l in text.split("\n") if l.strip()] or [" "]
    for _ in range(6):   # якщо не влазить по ширині — зменшуємо шрифт
        img = _render(lines, style, size)
        if img.width <= max_width + 40 or size <= 30:
            return img
        size = int(size * 0.9)
    return img


def _render(lines: list[str], style: dict, size: int) -> Image.Image:
    font = load_font(style.get("font", ""), size, TEXT_FALLBACKS)
    efont = _emoji_font(style, size)
    stroke = int(style.get("outline_width", 6))
    shadow = int(style.get("shadow_offset", 5))
    color = ImageColor.getrgb(style.get("color", "#FFFFFF"))
    ocolor = ImageColor.getrgb(style.get("outline_color", "#000000"))
    line_h = int(size * 1.18)
    pad = stroke + shadow + 8

    # ширина кожного рядка
    measured = []
    for line in lines:
        runs = split_runs(line)
        w = 0
        parts = []
        for frag, is_emoji in runs:
            if is_emoji and efont is not None:
                fw = int(size * 1.15) * len([c for c in frag if ord(c) > 0x2000 and c not in "\uFE0F\u200D"])
                fw = max(fw, int(size * 1.15))
            else:
                fw = int(font.getlength(frag))
            parts.append((frag, is_emoji, fw))
            w += fw
        measured.append((parts, w))
    W = max(w for _, w in measured) + 2 * pad
    H = line_h * len(lines) + 2 * pad
    text_layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    shadow_layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    dt = ImageDraw.Draw(text_layer)
    ds = ImageDraw.Draw(shadow_layer)
    for i, (parts, w) in enumerate(measured):
        x = (W - w) / 2
        y = pad + i * line_h
        for frag, is_emoji, fw in parts:
            if is_emoji and efont is not None:
                _draw_emoji(text_layer, frag, efont, int(x), int(y), size)
            else:
                ds.text((x + shadow, y + shadow), frag, font=font, fill=(0, 0, 0, int(style.get("shadow_alpha", 170))),
                        stroke_width=stroke, stroke_fill=(0, 0, 0, int(style.get("shadow_alpha", 170))))
                dt.text((x, y), frag, font=font, fill=color + (255,), stroke_width=stroke, stroke_fill=ocolor + (255,))
            x += fw
    shadow_layer = shadow_layer.filter(ImageFilter.GaussianBlur(max(1, shadow // 2)))
    return Image.alpha_composite(shadow_layer, text_layer)


def _draw_emoji(dst: Image.Image, frag: str, efont, x: int, y: int, size: int):
    try:
        tmp = Image.new("RGBA", (int(efont.size * 1.4 * max(1, len(frag))), int(efont.size * 1.4)), (0, 0, 0, 0))
        ImageDraw.Draw(tmp).text((0, 0), frag, font=efont, embedded_color=True)
        bbox = tmp.getbbox()
        if not bbox:
            return
        tmp = tmp.crop(bbox)
        k = (size * 1.05) / max(1, tmp.height)
        tmp = tmp.resize((max(1, int(tmp.width * k)), max(1, int(tmp.height * k))), Image.LANCZOS)
        dst.alpha_composite(tmp, (x, y + int(size * 0.08)))
    except Exception as e:  # шрифт без кольорових гліфів тощо
        if "emoji" not in _warned:
            log.warning("Не вдалося намалювати емодзі (%s) — вони будуть пропущені в MP4.", e)
            _warned.add("emoji")
