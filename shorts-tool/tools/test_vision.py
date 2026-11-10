"""Етап 1: перевірка зорового детектора WoT на записі + звіт.

Найпростіше: перетягніть файл запису на test_vision.bat і дайте відповіді на питання.

Командний рядок (для досвідчених):
  python tools/test_vision.py ЗАПИС.mp4 --profile wot --start 10:00 --end 15:00
         --reference tools/reference_wot_fv215b.json --offset 10:12 --debug
  --reanalyze  — не сканувати знову, а переаналізувати збережені показання
                 (після зміни порогів у профілі; працює за секунди).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shorts import events as E  # noqa: E402
from shorts.config import ROOT, ask_profile, load_profile, load_settings, output_dir, work_dir  # noqa: E402
from shorts.errors import UserError  # noqa: E402
from shorts.logs import setup_logging  # noqa: E402
from shorts.media import probe  # noqa: E402
from shorts.report import write_reports  # noqa: E402
from shorts.util import fmt_time, parse_time  # noqa: E402
from shorts.vision_detector import detect, load_cached_readings  # noqa: E402

log = logging.getLogger("test_vision")
DEFAULT_REF = ROOT / "tools" / "reference_wot_fv215b.json"


def ask(prompt: str, default: str = "") -> str:
    d = f" [Enter = {default}]" if default else ""
    return input(f"{prompt}{d}: ").strip() or default


def progress_printer():
    state = {"last": -1, "t0": time.time()}

    def cb(frac: float):
        pct = int(frac * 100)
        if pct != state["last"]:
            state["last"] = pct
            el = time.time() - state["t0"]
            eta = el / frac - el if frac > 0.01 else 0
            print(f"\r  Прогрес: {pct:3d}%   минуло {fmt_time(el, 0)}, залишилось ~{fmt_time(eta, 0)}   ",
                  end="", flush=True)
            if pct >= 100:
                print()
    return cb


def print_events(evs: list[E.Event], battles: list[dict]):
    print()
    print("=" * 96)
    print(f" Знайдено боїв: {len(battles)}")
    print("=" * 96)
    for b in battles:
        dmg = b["damage_counter"] if b["damage_counter"] is not None else b["damage_sum"]
        print(f"\n БІЙ {b['index']}: {fmt_time(b['start'])} – {fmt_time(b['end'])}  |  влучань: {b['hits']}, "
              f"шкода: {dmg}, знищено ворогів: {b['kills']}, отримав влучань: {b['damage_taken']}, "
              f"кінець: {b['ended_by']}")
        print(f"   {'час':>10}  {'подія':<16} {'значення':>9}  {'впевн.':>6}  {'джерело':<26} деталі")
        for e in b["events"]:
            val = "" if e.value is None else f"{e.value:+.0f}" if e.type == E.HIT else f"{e.value:.0f}"
            print(f"   {fmt_time(e.t):>10}  {e.label:<16} {val:>9}  {e.confidence:>6.2f}  {e.source:<26} {e.detail}")


def compare_reference(ref: dict, evs: list[E.Event], offset: float) -> dict:
    tol = float(ref.get("tolerance_s", 1.0))
    hits = [e for e in evs if e.type == E.HIT]
    used: set[int] = set()
    rows = []
    print()
    print("=" * 96)
    print(f" ПОРІВНЯННЯ З ЕТАЛОНОМ: {ref.get('name', '')}  (бій починається на {fmt_time(offset)} запису, "
          f"допуск ±{tol:.1f} с)")
    print("=" * 96)
    found = 0
    for ts, dmg in ref["hits"]:
        t = parse_time(ts) + offset
        cands = [(abs(h.t - t), i, h) for i, h in enumerate(hits) if i not in used]
        cands.sort(key=lambda c: c[0])
        best = cands[0] if cands else None
        ok = best is not None and best[0] <= tol
        if ok:
            used.add(best[1])
            found += 1
        h = best[2] if best else None
        dmg_ok = h is not None and h.value is not None and abs(h.value - dmg) <= 2
        status = "ЗНАЙДЕНО " if ok else "ПРОПУЩЕНО"
        near = (f"найближче: {fmt_time(h.t - offset)} ({h.t - t:+.1f} с), +{h.value:.0f}"
                if h is not None else "немає влучань поруч")
        print(f"  [{status}] влучання {ts:>5} +{dmg:<5} → {near}"
              f"{'  шкода збігається' if ok and dmg_ok else ('  ШКОДА ІНША' if ok else '')}")
        rows.append({"очікувано": ts, "шкода": dmg, "знайдено": ok,
                     "різниця_с": None if h is None else round(h.t - t, 2),
                     "знайдена_шкода": None if h is None else h.value})

    death_ok = False
    if ref.get("death"):
        td = parse_time(ref["death"]) + offset
        deaths = sorted([e for e in evs if e.type == E.DEATH], key=lambda e: abs(e.t - td))
        if deaths:
            d = deaths[0]
            death_ok = abs(d.t - td) <= tol
            print(f"  [{'ЗНАЙДЕНО ' if death_ok else 'НЕТОЧНО  '}] смерть {ref['death']} → "
                  f"{fmt_time(d.t - offset)} ({d.t - td:+.1f} с), джерело: {d.source}")
        else:
            print(f"  [ПРОПУЩЕНО] смерть {ref['death']} → не знайдено")

    # зайві влучання в межах бою
    t_end = offset + (parse_time(ref.get("death")) or 0) + 10
    extra = [h for i, h in enumerate(hits) if i not in used and offset - 5 <= h.t <= t_end]
    for h in extra:
        print(f"  [ЗАЙВЕ    ] влучання {fmt_time(h.t - offset)} +{h.value:.0f}  ({h.source}, {h.detail})")
    if ref.get("total_damage"):
        counters = [h.extra.get("counter") for h in hits if h.extra.get("counter") and offset - 5 <= h.t <= t_end]
        last = max(counters) if counters else None
        print(f"  Підсумок шкоди: очікувано {ref['total_damage']}, лічильник показав {last}")
        if last and last != ref["total_damage"]:
            print("    (різниця може бути від дрібної шкоди — тараном/пожежею — або від пропущеного влучання)")
    n = len(ref["hits"])
    passed = found == n and death_ok
    print("-" * 96)
    print(f"  РЕЗУЛЬТАТ: влучань знайдено {found} з {n}; смерть {'так' if death_ok else 'НІ'}; "
          f"зайвих влучань: {len(extra)}  →  {'ПРОЙДЕНО' if passed else 'НЕ ПРОЙДЕНО'}")
    print("=" * 96)
    return {"еталон": ref.get("name"), "зсув_с": offset, "влучання": rows, "знайдено": found, "всього": n,
            "смерть_знайдена": death_ok, "зайвих": len(extra), "пройдено": passed}


def main() -> int:
    ap = argparse.ArgumentParser(description="Перевірка зорового детектора WoT")
    ap.add_argument("source", nargs="?")
    ap.add_argument("--profile")
    ap.add_argument("--start", help="з якого моменту запису (сек або хв:сек)")
    ap.add_argument("--end", help="до якого моменту")
    ap.add_argument("--reference", help="файл еталону (JSON)")
    ap.add_argument("--offset", help="на якій секунді запису починається еталонний бій (0:00)")
    ap.add_argument("--debug", action="store_true", help="зберігати вирізані зони з розпізнаним текстом")
    ap.add_argument("--reanalyze", action="store_true", help="використати збережені показання (без сканування)")
    ap.add_argument("--ask", action="store_true", help="поставити питання в консолі")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    settings = load_settings()
    log_file = setup_logging("test_vision", work_dir(settings) / "logs", args.verbose)

    src = args.source or input("Перетягніть сюди файл запису і натисніть Enter: ").strip().strip('"')
    src = Path(src)
    info = probe(settings, src)
    print(f"\nЗапис: {info.path.name}  |  {info.width}×{info.height}, {info.fps:.0f} fps, "
          f"тривалість {fmt_time(info.duration, 0)}, аудіодоріжок: {info.audio_streams}")

    profile_id = args.profile or (ask_profile("wot") if args.ask else "wot")
    profile = load_profile(profile_id)
    if not profile["signals"].get("vision"):
        raise UserError(f"У профілі «{profile['name']}» зоровий детектор вимкнено (signals.vision = false).",
                        "Для перевірки етапу 1 оберіть профіль wot.")
    if not profile.get("zones_confirmed"):
        print("\n[УВАГА] Зони в профілі ще не обведені на вашому записі — розпізнавання може не працювати.\n"
              "        Спершу запустіть pick_zones.bat (README, крок 5).")

    start, end = parse_time(args.start) or 0.0, parse_time(args.end)
    ref_path = Path(args.reference) if args.reference else None
    offset = parse_time(args.offset)
    debug = args.debug
    reanalyze = args.reanalyze

    if args.ask:
        print("\nМожна обробити весь запис або лише відрізок (швидше для перевірки).")
        start = parse_time(ask("З якого моменту почати (сек або хв:сек)", "0")) or 0.0
        e = ask("До якого моменту (порожньо = до кінця)", "")
        end = parse_time(e) if e else None
        if ask("Це еталонний бій FV215b 183 з завдання? (т/н)", "н").lower().startswith(("т", "y", "д")):
            ref_path = DEFAULT_REF
            offset = parse_time(ask("На якій секунді ЗАПИСУ починається цей бій (момент 0:00 еталону)",
                                    fmt_time(start, 0)))
        debug = ask("Зберігати картинки зон для діагностики? (т/н)", "т").lower().startswith(("т", "y", "д"))

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    debug_dir = output_dir(settings) / "debug" / f"{src.stem}_{stamp}" if debug else None

    readings = load_cached_readings(settings, profile, info, start, end) if reanalyze else None
    if readings is not None:
        span = readings[-1]["t"] - readings[0]["t"]
        expected = ((end or info.duration) - start) * profile["vision"]["sample_fps"]
        if len(readings) < 0.9 * expected:
            print(f"\nЗбережене сканування неповне ({len(readings)} з ~{expected:.0f} кадрів) — доскановую.")
            readings = None
        else:
            print(f"\nВикористовую збережені показання ({len(readings)} шт., {fmt_time(span, 0)}).")
    if readings is None:
        print(f"\nСканую {fmt_time(start, 0)} – {fmt_time(end or info.duration, 0)} "
              f"({profile['vision']['sample_fps']} кадри/с). Перший запуск завантажує моделі — зачекайте.")

    t0 = time.time()
    evs, battles, readings = detect(settings, profile, info, start, end, debug_dir=debug_dir,
                                    progress=progress_printer(), readings=readings)
    print(f"\nГотово за {fmt_time(time.time() - t0, 0)}.")
    print_events(evs, battles)

    extra = {}
    if ref_path:
        with open(ref_path, encoding="utf-8") as f:
            ref = json.load(f)
        extra["перевірка_еталону"] = compare_reference(ref, evs, offset or start)

    rep_dir = output_dir(settings) / "reports"
    csv_p, json_p = write_reports(rep_dir, f"{src.stem}_vision_{stamp}", src, profile_id, evs, battles, extra)
    print(f"\nЗвіт (Excel): {csv_p}")
    print(f"Звіт (JSON):  {json_p}")
    if debug_dir and debug_dir.exists():
        print(f"Картинки зон: {debug_dir}  (у назві файлу — час і що розпізнано)")
    print(f"Лог:          {log_file}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except UserError as e:
        print(f"\n[ПОМИЛКА] {e}")
        logging.getLogger().debug("UserError", exc_info=True)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nПерервано. Відскановані частини збережено — наступний запуск продовжить з них.")
        sys.exit(1)
