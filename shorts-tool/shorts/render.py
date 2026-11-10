"""Рендер готового MP4 (1080×1920, 60 fps, H.264 через NVENC) з тим самим макетом, що й у DaVinci.

Для кожного шматка шортсу: crop+scale трьох шарів (гра, вебкамера, лічильники) з того ж
запису -> накладання на чорне тло -> склеювання шматків -> накладання картинок субтитрів
і заклику (PNG) у потрібний час -> кодування на відеокарті. Оригінал не змінюється.
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .errors import UserError
from .layout import Layout, dst_xy, ffmpeg_layer_filter
from .media import VideoInfo, find_tool

log = logging.getLogger("render")
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


@dataclass
class Overlay:
    png: Path
    start: float
    end: float
    x: int
    y: int


def _audio_chain(settings: dict, i: int, dur: float, label: str) -> str:
    rec = settings["recording"]
    fade = min(0.015, dur / 4)
    tail = f"aresample=48000,afade=t=in:d={fade:.3f},afade=t=out:st={max(0.0, dur - fade):.3f}:d={fade:.3f}"
    if rec["mode"] == "separate":
        g, m = rec["separate"]["game_audio_track"], rec["separate"]["mic_audio_track"]
        return f"[{i}:a:{g}][{i}:a:{m}]amix=inputs=2:normalize=0,{tail}[{label}]"
    return f"[{i}:a:{rec['mixed']['audio_track']}]{tail}[{label}]"


def build_command(settings: dict, info: VideoInfo, layout: Layout, segments: list[tuple[float, float]],
                  overlays: list[Overlay], out_path: Path, webcam: dict | None, encoder: str) -> list[str]:
    ffmpeg = find_tool(settings, "ffmpeg")
    hw = (settings.get("processing", {}).get("hwaccel") or "").strip()
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-nostdin"]
    W, H, fps = layout.width, layout.height, layout.fps
    inputs = 0
    seg_inputs = []
    for a, b in segments:
        d = b - a
        if hw:
            cmd += ["-hwaccel", hw]
        cmd += ["-ss", f"{a:.3f}", "-t", f"{d:.3f}", "-i", str(info.path)]
        rec_i = inputs
        inputs += 1
        cam_i = None
        if webcam is not None:
            cmd += ["-ss", f"{max(0.0, a + webcam.get('offset', 0.0)):.3f}", "-t", f"{d:.3f}", "-i", str(webcam["path"])]
            cam_i = inputs
            inputs += 1
        seg_inputs.append((rec_i, cam_i, d))
    ov_inputs = []
    for ov in overlays:
        cmd += ["-i", str(ov.png)]
        ov_inputs.append(inputs)
        inputs += 1

    f: list[str] = []
    concat_in = []
    for k, (rec_i, cam_i, d) in enumerate(seg_inputs):
        rec_layers = [l for l in layout.layers if l.source == "recording"]
        n = len(rec_layers)
        f.append(f"[{rec_i}:v]fps={fps},setpts=PTS-STARTPTS"
                 + (f",split={n}" + "".join(f"[r{k}_{j}]" for j in range(n)) if n > 1 else f"[r{k}_0]"))
        f.append(f"color=c=black:s={W}x{H}:r={fps}:d={d:.3f}[bg{k}]")
        cur = f"bg{k}"
        j_rec = 0
        for idx, layer in enumerate(sorted(layout.layers, key=lambda l: l.track)):
            if layer.source == "recording":
                src = f"r{k}_{j_rec}"
                j_rec += 1
            else:
                f.append(f"[{cam_i}:v]fps={fps},setpts=PTS-STARTPTS[c{k}]")
                src = f"c{k}"
            f.append(ffmpeg_layer_filter(layer, src, f"l{k}_{idx}"))
            x, y = dst_xy(layer)
            f.append(f"[{cur}][l{k}_{idx}]overlay={x}:{y}:shortest=1:eof_action=pass[o{k}_{idx}]")
            cur = f"o{k}_{idx}"
        f.append(f"[{cur}]trim=duration={d:.3f},setpts=PTS-STARTPTS[v{k}]")
        f.append(_audio_chain(settings, rec_i, d, f"a{k}"))
        concat_in.append(f"[v{k}][a{k}]")
    f.append("".join(concat_in) + f"concat=n={len(seg_inputs)}:v=1:a=1[vc][ac]")
    cur = "vc"
    for n_ov, (ov, inp) in enumerate(zip(overlays, ov_inputs)):
        nxt = f"t{n_ov}"
        f.append(f"[{cur}][{inp}:v]overlay={ov.x}:{ov.y}:enable='between(t,{ov.start:.3f},{ov.end:.3f})'"
                 f":eof_action=repeat[{nxt}]")
        cur = nxt
    f.append(f"[{cur}]format=yuv420p[vout]")
    cmd += ["-filter_complex", ";".join(f), "-map", "[vout]", "-map", "[ac]", "-r", str(fps)]

    r = settings["render"]
    if encoder == "h264_nvenc":
        cmd += ["-c:v", "h264_nvenc", "-preset", r["nvenc_preset"], "-rc", "vbr", "-cq", str(r["cq"]),
                "-b:v", "0", "-maxrate", r["max_bitrate"], "-bufsize", "40M", "-profile:v", "high"]
    else:
        cmd += ["-c:v", "libx264", "-preset", "medium", "-crf", str(r["x264_crf"]), "-profile:v", "high"]
    cmd += ["-c:a", "aac", "-b:a", r["audio_bitrate"], "-movflags", "+faststart", str(out_path)]
    return cmd


_nvenc_broken = False


def render_short(settings: dict, info: VideoInfo, layout: Layout, segments: list[tuple[float, float]],
                 overlays: list[Overlay], out_path: Path, webcam: dict | None = None) -> Path:
    global _nvenc_broken
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_name(out_path.stem + ".part.mp4")
    encoders = [settings["render"]["encoder"], settings["render"]["fallback_encoder"]]
    if _nvenc_broken:
        encoders = encoders[1:]
    last_err = ""
    for enc in encoders:
        cmd = build_command(settings, info, layout, segments, overlays, tmp, webcam, enc)
        log.debug("RENDER %s", " ".join(cmd))
        t0 = time.time()
        res = subprocess.run(cmd, capture_output=True, creationflags=_NO_WINDOW)
        if res.returncode == 0 and tmp.exists() and tmp.stat().st_size > 1000:
            tmp.replace(out_path)
            log.info("    MP4 готовий за %.0f с (%s): %s", time.time() - t0, enc, out_path.name)
            return out_path
        last_err = res.stderr.decode("utf-8", "replace")[-2000:]
        if enc == "h264_nvenc":
            log.warning("    NVENC не спрацював, рендерю на процесорі. Причина: %s", last_err.strip()[-300:])
            _nvenc_broken = True
    if tmp.exists():
        tmp.unlink()
    raise UserError(f"Не вдалося відрендерити {out_path.name}:\n{last_err}",
                    "Надішліть цей текст і лог розробнику. Таймлайни в DaVinci від цього не залежать.")
