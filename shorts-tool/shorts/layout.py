"""Геометрія вертикального шортсу 1080×1920 — ОДНА для DaVinci і для ffmpeg.

Кожен шар = «який прямокутник запису (src, пікселі запису) куди кладемо (dst, пікселі шортсу)».
  V1 гра        — центральна частина кадру, масштаб під висоту області гри;
  V2 вебкамера  — зона вебкамери, обрізана під пропорції вікна (заповнення без смуг);
  V3 лічильники — зона блоку лічильників, без спотворень (пропорції зберігаються).

DaVinci (Lua) перетворює ці прямокутники на Zoom/Pan/Tilt/Crop (див. resolve_params),
ffmpeg — на crop + scale + overlay. Тому готовий MP4 збігається з таймлайном.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass

from .config import zone_px

log = logging.getLogger("layout")


@dataclass
class Rect:
    x: float
    y: float
    w: float
    h: float

    @property
    def x1(self) -> float:
        return self.x + self.w

    @property
    def y1(self) -> float:
        return self.y + self.h

    def overlaps(self, o: "Rect") -> bool:
        return self.x < o.x1 and o.x < self.x1 and self.y < o.y1 and o.y < self.y1

    def as_list(self) -> list[float]:
        return [round(self.x, 3), round(self.y, 3), round(self.w, 3), round(self.h, 3)]


@dataclass
class Layer:
    name: str        # game / webcam / counters
    track: int       # 1, 2, 3 (V1..V3)
    source: str      # recording / webcam_file
    src: Rect        # пікселі джерела
    dst: Rect        # пікселі шортсу
    src_w: int
    src_h: int

    def to_dict(self) -> dict:
        d = asdict(self)
        d["src"] = self.src.as_list()
        d["dst"] = self.dst.as_list()
        return d


@dataclass
class Layout:
    width: int
    height: int
    fps: int
    preset: str
    layers: list[Layer]
    subtitles_y: int
    warnings: list[str]

    def to_dict(self) -> dict:
        return {"width": self.width, "height": self.height, "fps": self.fps, "preset": self.preset,
                "layers": [l.to_dict() for l in self.layers], "subtitles_y": self.subtitles_y,
                "warnings": self.warnings}


def _clamp_rect(r: Rect, W: float, H: float) -> Rect:
    w, h = min(r.w, W), min(r.h, H)
    x = min(max(0.0, r.x), W - w)
    y = min(max(0.0, r.y), H - h)
    return Rect(x, y, w, h)


def _cover(src: Rect, dst_aspect: float, focus_y: float = 0.5) -> Rect:
    """Обрізати src під пропорції dst (заповнення без смуг)."""
    a = src.w / src.h
    if a > dst_aspect:          # джерело ширше — обрізаємо боки
        w = src.h * dst_aspect
        return Rect(src.x + (src.w - w) / 2, src.y, w, src.h)
    h = src.w / dst_aspect      # джерело вище — обрізаємо верх/низ
    return Rect(src.x, src.y + (src.h - h) * focus_y, src.w, h)


def compute_layout(profile: dict, rec_w: int, rec_h: int, webcam_file: dict | None = None) -> Layout:
    lay = profile["layout"]
    W, H, fps = int(lay["output"]["width"]), int(lay["output"]["height"]), int(lay["output"]["fps"])
    preset = lay["preset"]
    if preset not in ("split", "overlay"):
        raise ValueError(f"Невідомий макет «{preset}» (має бути split або overlay)")
    p = lay[preset]
    zones = profile.get("zones") or {}
    warnings: list[str] = []
    layers: list[Layer] = []

    # ---------------- область гри
    if preset == "split":
        wh = int(p["webcam_height"])
        game_dst = Rect(0, wh, W, H - wh)
    else:
        game_dst = Rect(0, 0, W, H)
    zoom = float(p.get("game_zoom", 1.0)) or 1.0
    s = game_dst.h / rec_h * zoom
    s = max(s, game_dst.w / rec_w)             # не менше, ніж треба, щоб заповнити ширину
    gw, gh = game_dst.w / s, game_dst.h / s
    cx = rec_w / 2 + float(p.get("game_offset_x", 0.0)) * rec_w
    cy = rec_h / 2 + float(p.get("game_offset_y", 0.0)) * rec_h
    game_src = _clamp_rect(Rect(cx - gw / 2, cy - gh / 2, gw, gh), rec_w, rec_h)

    # місце вебкамери в кадрі гри не повинно потрапити у V1
    has_cam_in_frame = webcam_file is None and zones.get("webcam")
    if has_cam_in_frame and lay.get("avoid_webcam_in_game", True):
        cam = Rect(*zone_px(zones["webcam"], rec_w, rec_h))
        if game_src.overlaps(cam):
            if cam.x + cam.w / 2 < rec_w / 2:   # вебкамера зліва — зсуваємо гру праворуч
                game_src = _clamp_rect(Rect(cam.x1, game_src.y, game_src.w, game_src.h), rec_w, rec_h)
            else:
                game_src = _clamp_rect(Rect(cam.x - game_src.w, game_src.y, game_src.w, game_src.h), rec_w, rec_h)
            if game_src.overlaps(cam):
                # не вдалось зсунути — обрізаємо смугу з вебкамерою (по боках буде чорне)
                if cam.x + cam.w / 2 < rec_w / 2:
                    cut = cam.x1 - game_src.x
                    game_src = Rect(cam.x1, game_src.y, game_src.w - cut, game_src.h)
                    game_dst = Rect(game_dst.x + cut * s, game_dst.y, game_dst.w - cut * s, game_dst.h)
                else:
                    cut = game_src.x1 - cam.x
                    game_src = Rect(game_src.x, game_src.y, game_src.w - cut, game_src.h)
                    game_dst = Rect(game_dst.x, game_dst.y, game_dst.w - cut * s, game_dst.h)
                warnings.append("Вебкамера заходить у центральну частину гри — смугу з нею обрізано.")
            else:
                warnings.append("Центр гри трохи зсунуто, щоб вебкамера з кадру не потрапила у V1.")
    layers.append(Layer("game", 1, "recording", game_src, game_dst, rec_w, rec_h))

    # ---------------- вебкамера
    if preset == "split":
        cam_dst = Rect(0, 0, W, int(p["webcam_height"]))
    else:
        cam_dst = Rect(float(p["webcam_x"]), float(p["webcam_y"]), float(p["webcam_width"]), float(p["webcam_height"]))
    focus = float(p.get("webcam_focus_y", 0.5))
    if webcam_file is not None:
        ww, whh = int(webcam_file["width"]), int(webcam_file["height"])
        cam_src = _cover(Rect(0, 0, ww, whh), cam_dst.w / cam_dst.h, focus)
        layers.append(Layer("webcam", 2, "webcam_file", cam_src, cam_dst, ww, whh))
    elif zones.get("webcam"):
        cz = Rect(*zone_px(zones["webcam"], rec_w, rec_h))
        cam_src = _cover(cz, cam_dst.w / cam_dst.h, focus)
        layers.append(Layer("webcam", 2, "recording", cam_src, cam_dst, rec_w, rec_h))
        k = cam_dst.w / cam_src.w
        if k > 3.5:
            warnings.append(f"Вебкамера збільшується в {k:.1f} раза — може бути нечіткою. "
                            "Розгляньте окремий файл вебкамери (OBS Source Record).")
    else:
        warnings.append("У профілі немає зони вебкамери — шар V2 пропущено.")

    # ---------------- лічильники
    c = lay.get("counters", {})
    if c.get("enabled") and zones.get("counters_block"):
        cz = Rect(*zone_px(zones["counters_block"], rec_w, rec_h))
        bw = float(c["width"])
        bh = float(c.get("height") or 0)
        if bh <= 0:
            dst = Rect(float(c["x"]), float(c["y"]), bw, bw * cz.h / cz.w)
        else:   # вписати без спотворень у заданий прямокутник
            k = min(bw / cz.w, bh / cz.h)
            dst = Rect(float(c["x"]), float(c["y"]), cz.w * k, cz.h * k)
        layers.append(Layer("counters", 3, "recording", cz, dst, rec_w, rec_h))
    elif c.get("enabled"):
        warnings.append("Лічильники (V3) увімкнені, але зону counters_block не обведено — V3 пропущено.")

    return Layout(W, H, fps, preset, layers, int(lay.get("subtitles_y", 1450)), warnings)


# ------------------------------------------------------------------ ffmpeg

def _even(v: float) -> int:
    return max(2, int(round(v / 2)) * 2)


def ffmpeg_layer_filter(layer: Layer, in_label: str, out_label: str) -> str:
    """crop + scale одного шару (без overlay)."""
    sx, sy = int(round(layer.src.x)), int(round(layer.src.y))
    sw, sh = _even(layer.src.w), _even(layer.src.h)
    sw = min(sw, layer.src_w - sx - (layer.src_w - sx) % 2)
    sh = min(sh, layer.src_h - sy - (layer.src_h - sy) % 2)
    dw, dh = _even(layer.dst.w), _even(layer.dst.h)
    return f"[{in_label}]crop={sw}:{sh}:{sx}:{sy},scale={dw}:{dh}:flags=lanczos,setsar=1[{out_label}]"


def dst_xy(layer: Layer) -> tuple[int, int]:
    return int(round(layer.dst.x)), int(round(layer.dst.y))


# ------------------------------------------------------------------ DaVinci (дзеркало Lua-коду, для перевірки й логів)

def resolve_params(layer: Layer, tl_w: int, tl_h: int, mode: str = "fit", tilt_up_positive: bool = True) -> dict:
    """Значення Inspector (Zoom/Position/Crop) для шару.

    Модель Resolve: кадр джерела спершу вписується в таймлайн (mode: fit — повністю,
    fill — із заповненням, none — 1:1), отримуємо «вписане» зображення в центрі.
    Обрізка (Crop) — у пікселях вписаного зображення; Zoom масштабує навколо центру
    кадру; Position (Pan/Tilt) зсуває у пікселях таймлайну (Tilt > 0 — вгору).
    """
    sw, sh = layer.src_w, layer.src_h
    if mode == "fill":
        b = max(tl_w / sw, tl_h / sh)
    elif mode == "none":
        b = 1.0
    else:
        b = min(tl_w / sw, tl_h / sh)
    fw, fh = sw * b, sh * b
    z = (layer.dst.w / layer.src.w) / b
    u_c = (layer.src.x + layer.src.w / 2) * b
    v_c = (layer.src.y + layer.src.h / 2) * b
    du, dv = u_c - fw / 2, v_c - fh / 2
    X_c = layer.dst.x + layer.dst.w / 2
    Y_c = layer.dst.y + layer.dst.h / 2
    pan = X_c - tl_w / 2 - z * du
    tilt_down = Y_c - tl_h / 2 - z * dv
    return {
        "ZoomX": round(z, 4), "ZoomY": round(z, 4),
        "Pan": round(pan, 2), "Tilt": round(-tilt_down if tilt_up_positive else tilt_down, 2),
        "CropLeft": round(layer.src.x * b, 2),
        "CropRight": round((sw - layer.src.x1) * b, 2),
        "CropTop": round(layer.src.y * b, 2),
        "CropBottom": round((sh - layer.src.y1) * b, 2),
    }
