"""Попередній перегляд макета шортсу на одному кадрі — щоб перевірити рамки до довгої обробки.

Перетягніть запис на preview_layout.bat, оберіть профіль і секунду. Буде створено
output\\preview_<профіль>.png — кадр 1080×1920 саме так, як він виглядатиме в шортсі
(гра, вебкамера, лічильники, приклад субтитру й заклику).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shorts.config import ask_profile, load_profile, load_settings, output_dir, work_dir  # noqa: E402
from shorts.errors import UserError  # noqa: E402
from shorts.layout import compute_layout  # noqa: E402
from shorts.logs import setup_logging  # noqa: E402
from shorts.media import find_tool, probe, run  # noqa: E402
from shorts.pipeline import build_overlays, find_webcam_file  # noqa: E402
from shorts.render import render_short  # noqa: E402
from shorts.subtitles import Cue  # noqa: E402
from shorts.util import parse_time  # noqa: E402


def main() -> int:
    settings = load_settings()
    setup_logging("preview_layout", work_dir(settings) / "logs")
    src = Path((sys.argv[1] if len(sys.argv) > 1 else input("Перетягніть сюди запис і натисніть Enter: ")).strip().strip('"'))
    info = probe(settings, src)
    profile_id = ask_profile("wot")
    prof = load_profile(profile_id)
    ans = input("Секунда кадру (напр. 95 або 1:35) [Enter = 60]: ").strip()
    t = parse_time(ans) if ans else min(60.0, info.duration / 2)

    webcam = None
    if settings["recording"]["mode"] == "separate":
        w = find_webcam_file(settings, src)
        if w:
            wi = probe(settings, w)
            webcam = {"path": str(w), "width": wi.width, "height": wi.height, "fps": wi.fps, "offset": 0.0}
    layout = compute_layout(prof, info.width, info.height, webcam)
    for w in layout.warnings:
        print(f"[УВАГА] {w}")
    tmp = work_dir(settings) / "preview"
    overlays = build_overlays(settings, [Cue(0, 2, "Приклад субтитру\nдва рядки 😱")],
                              {"text": "Тут буде ваш заклик 🔥", "start": 0, "dur": 2}, layout, tmp)
    mp4 = tmp / "preview.mp4"
    render_short(settings, info, layout, [(t, t + 0.5)], overlays, mp4, webcam)
    out = output_dir(settings) / f"preview_{profile_id}.png"
    run([find_tool(settings, "ffmpeg"), "-hide_banner", "-loglevel", "error", "-y", "-ss", "0.1", "-i", str(mp4),
         "-frames:v", "1", str(out)])
    print(f"\nГотово: {out}")
    if os.name == "nt":
        os.startfile(str(out))  # type: ignore[attr-defined]
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except UserError as e:
        print(f"\n[ПОМИЛКА] {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(1)
