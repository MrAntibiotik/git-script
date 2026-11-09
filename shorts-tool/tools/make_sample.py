"""Вирізати зразок звуку гри з запису в бібліотеку samples/<гра>/<категорія>/.

Перетягніть запис на make_sample.bat і відповідайте на питання:
  - секунда початку й кінця звуку (напр. 1:51.2 і 1:52.0) — коротко, 0,3–2 с;
  - категорія (пробиття, знищено, …) — номер зі списку або нова назва;
  - звідки брати: мікс (як є), доріжка гри (після розділення) або доріжка голосу.
Порада: вибирайте моменти, коли ви мовчите, — тоді зразок чистий.
Для голосу екіпажу («Пробиття!») — мікс або доріжка голосу (нейромережа відносить
мову екіпажу до «голосу»); для пострілів і вибухів — доріжка гри.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402

from shorts import events as E  # noqa: E402
from shorts.audio import SR_GAME, SR_SEP, Separator, read_audio, resample  # noqa: E402
from shorts.audio_detector import samples_root  # noqa: E402
from shorts.config import (ask_profile, load_profile, load_profile_raw, load_settings,  # noqa: E402
                           save_profile_raw)
from shorts.errors import UserError  # noqa: E402
from shorts.logs import setup_logging  # noqa: E402
from shorts.config import work_dir  # noqa: E402
from shorts.media import probe  # noqa: E402
from shorts.util import fmt_time, parse_time  # noqa: E402

TYPES = [(E.HIT, "влучання"), (E.KILL, "знищення ворога"), (E.DAMAGE_TAKEN, "отримав шкоду"),
         (E.GAME_SOUND, "просто цікавий звук")]


def ask(prompt: str, default: str = "") -> str:
    d = f" [Enter = {default}]" if default else ""
    return input(f"{prompt}{d}: ").strip() or default


def choose_category(profile_id: str, profile: dict) -> str:
    cats = list(profile["game_audio"]["categories"].keys())
    print("\nКатегорії:")
    for i, c in enumerate(cats, 1):
        t = profile["game_audio"]["categories"][c].get("type")
        print(f"  {i}. {c}  ({dict(TYPES).get(t, t)})")
    print("  або введіть назву нової категорії")
    while True:
        ans = ask("Категорія")
        if ans.isdigit() and 1 <= int(ans) <= len(cats):
            return cats[int(ans) - 1]
        name = re.sub(r"[^\w\-]+", "_", ans.strip().lower()).strip("_")
        if not name:
            continue
        if name in cats:
            return name
        print(f"Нова категорія «{name}». Що означає цей звук?")
        for i, (_, title) in enumerate(TYPES, 1):
            print(f"  {i}. {title}")
        k = ask("Номер", "1")
        typ = TYPES[int(k) - 1][0] if k.isdigit() and 1 <= int(k) <= len(TYPES) else E.GAME_SOUND
        raw = load_profile_raw(profile_id)
        raw.setdefault("game_audio", {}).setdefault("categories", {})[name] = {"type": typ, "weight": 1.0}
        save_profile_raw(profile_id, raw)
        profile["game_audio"]["categories"][name] = {"type": typ, "weight": 1.0}
        print(f"Категорію «{name}» додано в профіль.")
        return name


def main() -> int:
    settings = load_settings()
    setup_logging("make_sample", work_dir(settings) / "logs")
    src = Path((sys.argv[1] if len(sys.argv) > 1 else
                input("Перетягніть сюди файл запису і натисніть Enter: ")).strip().strip('"'))
    info = probe(settings, src)
    profile_id = ask_profile("wot")
    profile = load_profile(profile_id)
    rec = settings["recording"]
    separator = None

    print("\nЗвідки брати звук:\n  1. мікс (як записано) — для голосу екіпажу, коли ви мовчите\n"
          "  2. доріжка гри (нейромережа прибирає ваш голос) — для пострілів, вибухів\n"
          "  3. доріжка голосу (нейромережа) — для голосу екіпажу, якщо ви говорите поверх")
    source_kind = ask("Номер", "1")

    while True:
        print()
        t0 = parse_time(ask("Початок звуку (сек або хв:сек, напр. 1:51.2)"))
        t1 = parse_time(ask("Кінець звуку", fmt_time((t0 or 0) + 1.0)))
        if t0 is None or t1 is None or t1 <= t0:
            print("Кінець має бути пізніше за початок.")
            continue
        if t1 - t0 > 3.0:
            print("Зразок довший за 3 с — візьму лише перші 3 с.")
            t1 = t0 + 3.0
        cat = choose_category(profile_id, profile)

        if rec["mode"] == "separate":
            track = rec["separate"]["mic_audio_track"] if source_kind == "3" else rec["separate"]["game_audio_track"]
            y = read_audio(settings, info.path, t0, t1 - t0, SR_GAME, 1, track=track)
        elif source_kind in ("2", "3"):
            if separator is None:
                separator = Separator(settings)
            pad = 2.0
            a0 = max(0.0, t0 - pad)
            stereo = read_audio(settings, info.path, a0, (t1 - a0) + pad, SR_SEP, 2,
                                track=rec["mixed"]["audio_track"])
            voice, game = separator.separate(stereo)
            part = voice if source_kind == "3" else game
            s0 = int((t0 - a0) * SR_SEP)
            s1 = s0 + int((t1 - t0) * SR_SEP)
            y = resample(part[s0:s1], SR_SEP, SR_GAME)
        else:
            y = read_audio(settings, info.path, t0, t1 - t0, SR_GAME, 1, track=rec["mixed"]["audio_track"])

        if y.size == 0 or float(np.abs(y).max()) < 1e-4:
            print("У цьому відрізку тиша — перевірте час.")
            continue
        import soundfile as sf
        d = samples_root(profile) / cat
        d.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^\w\-]+", "_", src.stem)[:30]
        out = d / f"{cat}_{safe}_{int(t0 * 10):06d}.wav"
        sf.write(str(out), y.astype(np.float32), SR_GAME, subtype="PCM_16")
        print(f"Збережено: {out}  ({len(y) / SR_GAME:.2f} с)")
        if os.name == "nt" and ask("Прослухати? (т/н)", "н").lower().startswith(("т", "y")):
            os.startfile(str(out))  # type: ignore[attr-defined]
        if not ask("Ще один зразок? (т/н)", "т").lower().startswith(("т", "y", "д")):
            break
    print("\nГотово. Нові зразки використаються при наступному запуску test_audio.bat.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except UserError as e:
        print(f"\n[ПОМИЛКА] {e}")
        sys.exit(1)
    except (KeyboardInterrupt, EOFError):
        sys.exit(1)
