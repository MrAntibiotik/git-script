"""Етап 2: перевірка звуку — розділення голос/гра, звуки гри зі зразків, емоції,
об'єднання з подіями з екрану (для WoT) і score кожного моменту.

Найпростіше: перетягніть запис на test_audio.bat і дайте відповіді на питання.

Командний рядок:
  python tools/test_audio.py ЗАПИС.mp4 --profile wot --start 10:00 --end 15:00 --save-stems
  --no-separation  працювати по міксу (без Demucs)
  --no-vision      не запускати зоровий детектор (лише звук)
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402

from shorts import events as E  # noqa: E402
from shorts.audio import SR_GAME, SR_VOICE, AudioPrep  # noqa: E402
from shorts.audio_detector import GameSoundDetector, describe as describe_sounds  # noqa: E402
from shorts.config import ask_profile, load_profile, load_settings, output_dir, work_dir  # noqa: E402
from shorts.errors import UserError  # noqa: E402
from shorts.fusion import assign_to_battles, fuse, split_by_gaps  # noqa: E402
from shorts.logs import setup_logging  # noqa: E402
from shorts.media import probe  # noqa: E402
from shorts.report import write_reports  # noqa: E402
from shorts.util import fmt_time, parse_time  # noqa: E402
from shorts.voice_detector import VoiceDetector, count_loud_spikes, describe as describe_voice  # noqa: E402

log = logging.getLogger("test_audio")


def ask(prompt: str, default: str = "") -> str:
    d = f" [Enter = {default}]" if default else ""
    return input(f"{prompt}{d}: ").strip() or default


def yes(ans: str) -> bool:
    return ans.lower().startswith(("т", "y", "д"))


def save_stems(chunks, out_dir: Path, base: str) -> list[Path]:
    import soundfile as sf
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for stem, sr in (("voice", SR_VOICE), ("game", SR_GAME)):
        data = np.concatenate([c.load(stem)[0] for c in chunks])
        p = out_dir / f"{base}_{'голос' if stem == 'voice' else 'гра'}.wav"
        sf.write(str(p), data, sr, subtype="PCM_16")
        paths.append(p)
    return paths


def print_battles(battles: list[dict], vision_on: bool):
    print()
    print("=" * 100)
    print(f" {'Боїв' if vision_on else 'Матчів/скупчень'}: {len(battles)}")
    print("=" * 100)
    for b in battles:
        ms = [e for e in b["events"] if e.extra.get("moment")]
        print(f"\n {'БІЙ' if vision_on else 'СКУПЧЕННЯ'} {b['index']}: {fmt_time(b['start'])} – {fmt_time(b['end'])}"
              f"  |  моментів: {len(ms)}"
              + (f", влучань: {b['hits']}, кінець: {b['ended_by']}" if vision_on else ""))
        print(f"   {'час':>10}  {'score':>5}  {'подія':<16} {'знач.':>6}  {'джерело':<24} чому / деталі")
        for e in b["events"]:
            if e.type in (E.BATTLE_START, E.BATTLE_END):
                continue
            if not e.extra.get("moment") and e.type in (E.GAME_SOUND, E.VOICE):
                mark = "   ·"   # звук/голос, що лише підтвердив або дав бонус
            else:
                mark = f"{e.extra.get('score', 0):5.1f}"
            val = "" if e.value is None else f"{e.value:.0f}"
            label = e.label
            if e.type == E.VOICE:
                label = f"Голос: {e.extra.get('kind')}"
            elif e.type == E.GAME_SOUND:
                label = f"Звук: {e.extra.get('category')}"
            why = e.extra.get("why") or e.detail
            print(f"   {fmt_time(e.t):>10}  {mark:>5}  {label:<16} {val:>6}  {e.source:<24} {why}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Перевірка звукових детекторів")
    ap.add_argument("source", nargs="?")
    ap.add_argument("--profile")
    ap.add_argument("--start")
    ap.add_argument("--end")
    ap.add_argument("--no-separation", action="store_true")
    ap.add_argument("--no-vision", action="store_true")
    ap.add_argument("--save-stems", action="store_true")
    ap.add_argument("--ask", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    settings = load_settings()
    log_file = setup_logging("test_audio", work_dir(settings) / "logs", args.verbose)
    src = Path(args.source or input("Перетягніть сюди файл запису і натисніть Enter: ").strip().strip('"'))
    info = probe(settings, src)
    print(f"\nЗапис: {info.path.name}  |  тривалість {fmt_time(info.duration, 0)}, аудіодоріжок: {info.audio_streams}, "
          f"режим запису: {settings['recording']['mode']}")
    profile_id = args.profile or (ask_profile("wot") if args.ask else "wot")
    profile = load_profile(profile_id)

    start, end = parse_time(args.start) or 0.0, parse_time(args.end)
    save = args.save_stems
    no_sep = args.no_separation
    if args.ask:
        print("\nДля перевірки візьміть відрізок 5–30 хв (весь запис теж можна, але довше).")
        start = parse_time(ask("З якого моменту почати (сек або хв:сек)", "0")) or 0.0
        e = ask("До якого моменту (порожньо = до кінця)", "")
        end = parse_time(e) if e else None
        save = yes(ask("Зберегти доріжки «голос» і «гра» у WAV, щоб послухати якість розділення? (т/н)", "т"))
    end_t = info.duration if end is None else min(end, info.duration)
    if save and end_t - start > 40 * 60:
        print("  (Відрізок довший за 40 хв — WAV не зберігаю, щоб не займати місце.)")
        save = False

    t_all = time.time()
    print(f"\n[1/4] Підготовка звуку {fmt_time(start, 0)} – {fmt_time(end_t, 0)} "
          f"({'без розділення' if no_sep else 'розділення голос/гра'})…")
    prep = AudioPrep(settings, profile, info, force_mix=no_sep)
    chunks = prep.prepare(start, end)
    separated = all(c.separated for c in chunks)

    print("[2/4] Пошук звуків гри…")
    gsd = GameSoundDetector(settings, profile)
    sounds = gsd.detect(chunks)
    print("[3/4] Аналіз вашого голосу…")
    # Звуки екіпажу, знайдені на доріжці голосу, — не ваші емоції.
    exclude = [(s.t, s.t + s.extra.get("duration", 0.5)) for s in sounds if s.extra.get("stem") == "voice"]
    voices = VoiceDetector(profile).detect(chunks, exclude=exclude)
    spikes = count_loud_spikes(chunks)

    vision_on = profile["signals"].get("vision") and not args.no_vision
    vision_events, battles = [], []
    if vision_on:
        print("[4/4] Зоровий детектор (використовую збережене сканування, якщо є)…")
        from shorts.vision_detector import detect
        vision_events, battles, _ = detect(settings, profile, info, start, end)
    else:
        print("[4/4] Зоровий детектор вимкнено — лише звук і голос.")
        if not profile["scoring"]["audio_only_moments"]:
            # Щоб побачити, що знайшов звук, показуємо звукові події як моменти.
            profile["scoring"]["audio_only_moments"] = True
            profile["scoring"]["voice_only_moments"] = True
            print("      (Для перевірки звукові події показано як моменти. У робочому режимі для цієї гри "
                  "моментами є лише події з екрану.)")

    all_events = fuse(vision_events, sounds, voices, profile)
    if vision_on:
        assign_to_battles(battles, sounds + voices)
    else:
        battles = split_by_gaps(all_events, profile["battles"]["split_gap_s"], start, end_t)
    print(f"\nГотово за {fmt_time(time.time() - t_all, 0)}.")
    print_battles(battles, vision_on)

    print()
    print("=" * 100)
    print(" ПІДСУМОК ЗВУКУ")
    print("=" * 100)
    print(f"  Розділення голос/гра: {'так (Demucs)' if separated else 'НІ — працював по міксу зі зниженими порогами'}")
    print(f"  Звуки гри зі зразків: {describe_sounds([s for s in sounds if s.extra.get('category') != 'гучність'])}")
    if profile["game_audio"]["loudness_spikes"]["enabled"]:
        print(f"  Гучні звуки гри: {sum(1 for s in sounds if s.extra.get('category') == 'гучність')}")
    print(f"  Ваш голос: {describe_voice(voices)}")
    print(f"  Для порівняння — просто «сплесків гучності» в міксі: {spikes}")
    if vision_on:
        vm = [e for e in all_events if e.extra.get("moment") and e.type in (E.HIT, E.KILL, E.DEATH, E.DAMAGE_TAKEN)]
        conf = sum(1 for e in vm if e.extra.get("sound_confirmed"))
        emo = sum(1 for e in vm if e.extra.get("emotion"))
        print(f"  Подій з екрану: {len(vm)}, з них підтверджено звуком: {conf}, з вашою реакцією: {emo}")
        if not gsd.templates:
            print("  (Зразків звуків ще немає — підтвердження звуком з'явиться, коли додасте їх через make_sample.bat.)")

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    rep_dir = output_dir(settings) / "reports"
    moments = sorted(all_events, key=lambda e: e.t)
    for e in moments:
        e.extra.setdefault("score", 0.0)
    csv_p, json_p = write_reports(rep_dir, f"{src.stem}_audio_{stamp}", src, profile_id, moments, battles,
                                  {"розділення": separated, "сплесків_у_міксі": spikes})
    print(f"\nЗвіт (Excel): {csv_p}\nЗвіт (JSON):  {json_p}")
    if save:
        paths = save_stems(chunks, output_dir(settings) / "audio", f"{src.stem}_{int(start)}-{int(end_t)}")
        print("Доріжки для прослуховування:\n  " + "\n  ".join(str(p) for p in paths))
    print(f"Лог:          {log_file}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except UserError as e:
        print(f"\n[ПОМИЛКА] {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nПерервано. Готові частини збережено — наступний запуск продовжить з них.")
        sys.exit(1)
