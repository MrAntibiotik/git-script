"""Розпізнавання тексту (EasyOCR, локально, на відеокарті NVIDIA).

Працюємо лише з маленькими вирізаними зонами кадру, тому це швидко.
Моделі завантажуються один раз у папку models/easyocr.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import cv2
import numpy as np

from .config import models_dir
from .errors import UserError

log = logging.getLogger("ocr")

DIGITS = "0123456789 "
HP_CHARS = "0123456789/ "


@dataclass
class OcrBox:
    text: str
    conf: float
    x0: int
    y0: int
    x1: int
    y1: int

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2

    @property
    def h(self) -> int:
        return self.y1 - self.y0


class OcrEngine:
    def __init__(self, settings: dict):
        self.cfg = settings["ocr"]
        try:
            import easyocr  # noqa: F401
        except ImportError:
            raise UserError("Не встановлено бібліотеку EasyOCR.",
                            "Запустіть install.bat ще раз (README, крок 3).")
        import easyocr
        gpu = bool(self.cfg.get("gpu", True))
        if gpu:
            try:
                import torch
                if not torch.cuda.is_available():
                    log.warning("CUDA недоступна — розпізнавання тексту піде на процесорі (повільніше). "
                                "Запустіть check_setup.bat, щоб з'ясувати причину.")
                    gpu = False
            except Exception:
                gpu = False
        store = models_dir(settings) / "easyocr"
        store.mkdir(parents=True, exist_ok=True)
        log.info("Завантажую модель розпізнавання тексту (%s, %s)…",
                 "+".join(self.cfg["languages"]), "GPU" if gpu else "CPU")
        try:
            self.reader = easyocr.Reader(self.cfg["languages"], gpu=gpu,
                                         model_storage_directory=str(store),
                                         user_network_directory=str(store),
                                         verbose=False)
        except Exception as e:
            raise UserError(f"Не вдалося запустити EasyOCR: {e}",
                            "Якщо це перший запуск — потрібен інтернет, щоб завантажити моделі "
                            "(~100 МБ). Далі все працює без інтернету.")
        self.gpu = gpu
        self.calls = 0

    @staticmethod
    def _prep(img: np.ndarray, scale: float) -> np.ndarray:
        if scale and abs(scale - 1.0) > 1e-3:
            interp = cv2.INTER_CUBIC if scale > 1 else cv2.INTER_AREA
            img = cv2.resize(img, None, fx=scale, fy=scale, interpolation=interp)
        return cv2.cvtColor(img, cv2.COLOR_BGR2RGB)

    def read_boxes(self, img: np.ndarray, scale: float = 1.0, allowlist: str | None = None,
                   min_conf: float | None = None) -> list[OcrBox]:
        """Текстові фрагменти з координатами у системі ВХІДНОГО зображення."""
        if img is None or img.size == 0:
            return []
        self.calls += 1
        min_conf = self.cfg.get("min_confidence", 0.25) if min_conf is None else min_conf
        rgb = self._prep(img, scale)
        kwargs = {"detail": 1, "paragraph": False}
        if allowlist:
            kwargs["allowlist"] = allowlist
        try:
            res = self.reader.readtext(rgb, **kwargs)
        except Exception as e:  # дуже рідко: дивний розмір зони тощо
            log.debug("OCR error: %s", e)
            return []
        out = []
        s = scale or 1.0
        for pts, text, conf in res:
            if conf < min_conf or not str(text).strip():
                continue
            xs = [p[0] for p in pts]
            ys = [p[1] for p in pts]
            out.append(OcrBox(str(text).strip(), float(conf),
                              int(min(xs) / s), int(min(ys) / s), int(max(xs) / s), int(max(ys) / s)))
        return out

    def read_line(self, img: np.ndarray, allowlist: str | None = None, scale: float | None = None) -> str:
        """Весь текст зони одним рядком (фрагменти зліва направо)."""
        sc = self.cfg.get("digit_upscale", 2.0) if scale is None else scale
        boxes = self.read_boxes(img, scale=sc, allowlist=allowlist)
        boxes.sort(key=lambda b: b.x0)
        return " ".join(b.text for b in boxes).strip()


def group_lines(boxes: list[OcrBox]) -> list[list[OcrBox]]:
    """Групує фрагменти тексту в рядки за вертикаллю."""
    lines: list[list[OcrBox]] = []
    for b in sorted(boxes, key=lambda b: b.cy):
        for line in lines:
            ref = line[0]
            if abs(b.cy - ref.cy) <= max(4, 0.6 * max(ref.h, b.h)):
                line.append(b)
                break
        else:
            lines.append([b])
    for line in lines:
        line.sort(key=lambda b: b.x0)
    return lines
