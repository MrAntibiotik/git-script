"""Перевірка встановлення: Python, ffmpeg (NVENC, CUDA), PyTorch+CUDA, EasyOCR, профілі.

Запуск: check_setup.bat (або venv\\Scripts\\python tools\\check_setup.py)
"""

from __future__ import annotations

import platform
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shorts.config import list_profiles, load_profile, load_settings, models_dir  # noqa: E402
from shorts.errors import UserError  # noqa: E402

OK, WARN, FAIL = "[ OK ]", "[УВАГА]", "[ПОМИЛКА]"
problems = 0


def say(status: str, text: str, hint: str = ""):
    global problems
    if status == FAIL:
        problems += 1
    print(f"{status} {text}")
    if hint:
        print(f"         Що зробити: {hint}")


def main():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    print("=" * 70)
    print(" Перевірка встановлення інструмента автонарізки шортсів")
    print("=" * 70)

    # Python
    v = sys.version_info
    bits = platform.architecture()[0]
    if v >= (3, 10) and bits == "64bit":
        say(OK, f"Python {v.major}.{v.minor}.{v.micro} ({bits})")
    else:
        say(FAIL, f"Python {v.major}.{v.minor} ({bits}) — потрібен 3.10–3.12, 64-bit",
            "Встановіть Python 3.11 64-bit (README, крок 1).")

    # Налаштування
    try:
        settings = load_settings()
        say(OK, "config/settings.json прочитано")
    except UserError as e:
        say(FAIL, str(e))
        return finish()

    # ffmpeg
    from shorts.media import find_tool, run
    try:
        ffmpeg = find_tool(settings, "ffmpeg")
        ffprobe = find_tool(settings, "ffprobe")
        ver = run([ffmpeg, "-hide_banner", "-version"]).stdout.decode("utf-8", "replace").splitlines()[0]
        say(OK, f"ffmpeg: {ffmpeg}\n         {ver}")
        say(OK, f"ffprobe: {ffprobe}")
        hw = run([ffmpeg, "-hide_banner", "-hwaccels"]).stdout.decode("utf-8", "replace")
        if "cuda" in hw:
            say(OK, "ffmpeg підтримує декодування на відеокарті (cuda)")
        else:
            say(WARN, "ffmpeg без підтримки cuda — відео декодуватиметься процесором (повільніше)",
                "Завантажте збірку ffmpeg з README (BtbN, win64-gpl).")
        test = run([ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                    "color=c=black:s=256x256:d=1:r=30", "-c:v", "h264_nvenc", "-f", "null", "-"], check=False)
        if test.returncode == 0:
            say(OK, "Кодування NVENC (H.264 на відеокарті) працює")
        else:
            err = test.stderr.decode("utf-8", "replace").strip().splitlines()[-1:] or [""]
            say(WARN, f"NVENC не працює ({err[0][:120]}) — потрібно для рендеру MP4 (етап 4)",
                "Оновіть драйвер NVIDIA (GeForce Experience / nvidia.com) і перезавантажте ПК.")
    except UserError as e:
        say(FAIL, e.message, e.hint or "")

    # PyTorch + CUDA
    try:
        import torch
        if torch.cuda.is_available():
            name = torch.cuda.get_device_name(0)
            mem = torch.cuda.get_device_properties(0).total_memory / 1024 ** 3
            say(OK, f"PyTorch {torch.__version__}, CUDA працює: {name} ({mem:.0f} ГБ)")
            # RTX 50xx (Blackwell, sm_120) потребує PyTorch, зібраного з CUDA 12.8+.
            # Стара збірка каже «CUDA доступна», але падає на першому ж обчисленні.
            major, minor = torch.cuda.get_device_capability(0)
            arch = f"sm_{major}{minor}"
            archs = torch.cuda.get_arch_list()
            try:
                x = torch.ones(256, device="cuda")
                float((x * 2).sum().item())
                if archs and arch not in archs and not any(a.startswith("compute_") for a in archs):
                    say(WARN, f"PyTorch не має готових ядер для {arch} ({name}), але тест пройшов")
                else:
                    say(OK, f"Обчислення на відеокарті працюють ({arch}, CUDA {torch.version.cuda})")
            except Exception as e:
                say(FAIL, f"PyTorch не підтримує вашу відеокарту {name} ({arch}): {str(e)[:120]}",
                    "Для RTX 50xx потрібен PyTorch з CUDA 12.8: видаліть папку venv і запустіть install.bat "
                    "знову (він ставить саме таку версію). Драйвер NVIDIA — 570 або новіший.")
        else:
            say(FAIL, f"PyTorch {torch.__version__} встановлено, але CUDA недоступна",
                "1) Оновіть драйвер NVIDIA. 2) Запустіть install.bat ще раз — він поставить PyTorch з CUDA. "
                "Якщо у версії PyTorch вище є «+cpu» — це версія без відеокарти.")
    except ImportError:
        say(FAIL, "PyTorch не встановлено", "Запустіть install.bat (README, крок 3).")

    # Бібліотеки
    for mod, title in (("cv2", "OpenCV"), ("numpy", "NumPy"), ("PIL", "Pillow"), ("easyocr", "EasyOCR"),
                       ("demucs", "Demucs (розділення звуку)"), ("librosa", "librosa (аналіз звуку)"),
                       ("soundfile", "soundfile"), ("scipy", "SciPy")):
        try:
            m = __import__(mod)
            say(OK, f"{title} {getattr(m, '__version__', '')}")
        except ImportError:
            say(FAIL, f"{title} не встановлено", "Запустіть install.bat ще раз.")

    try:
        import tkinter  # noqa: F401
        say(OK, "tkinter (вікна інструмента)")
    except ImportError:
        say(FAIL, "tkinter відсутній", "Перевстановіть Python з галочкою «tcl/tk and IDLE».")

    # Моделі EasyOCR (завантаження один раз)
    if "--no-ocr" not in sys.argv:
        try:
            from shorts.ocr import OcrEngine
            print("\n… Завантажую/перевіряю моделі розпізнавання тексту (перший раз ~100 МБ, потрібен інтернет)…")
            eng = OcrEngine(settings)
            import numpy as np
            import cv2
            img = np.zeros((60, 220, 3), np.uint8)
            cv2.putText(img, "1620", (10, 45), cv2.FONT_HERSHEY_SIMPLEX, 1.4, (255, 255, 255), 3)
            text = eng.read_line(img, allowlist="0123456789 ")
            if "1620" in text.replace(" ", ""):
                say(OK, f"EasyOCR працює ({'GPU' if eng.gpu else 'CPU'}), тест «1620» → «{text}»; "
                        f"моделі: {models_dir(settings) / 'easyocr'}")
            else:
                say(WARN, f"EasyOCR запустився, але тест прочитав «{text}» замість «1620»")
        except UserError as e:
            say(FAIL, e.message, e.hint or "")

    # Модель розділення звуку (завантаження один раз)
    if "--no-sep" not in sys.argv and settings["separation"]["enabled"] \
            and settings["recording"]["mode"] == "mixed":
        try:
            import numpy as np
            from shorts.audio import Separator
            print("\n… Завантажую/перевіряю модель розділення звуку (перший раз ~80 МБ)…")
            import time as _t
            sep = Separator(settings)
            noise = (np.random.default_rng(0).normal(0, 0.05, (44100 * 10, 2))).astype(np.float32)
            t0 = _t.time()
            v, g = sep.separate(noise)
            dt = _t.time() - t0
            say(OK, f"Розділення звуку працює ({sep.device.upper()}): 10 с звуку за {dt:.1f} с "
                    f"(≈ ×{10 / max(dt, 1e-3):.0f} від реального часу)")
        except UserError as e:
            say(FAIL, e.message, e.hint or "")
        except Exception as e:
            say(FAIL, f"Розділення звуку не запустилось: {e}",
                "Надішліть цей текст розробнику. Тимчасово можна вимкнути: settings.json → separation.enabled = false.")

    # Профілі
    print()
    for name in list_profiles():
        try:
            p = load_profile(name)
            if p.get("zones_confirmed"):
                say(OK, f"Профіль «{p['name']}» ({name}): зони перевірені")
            else:
                say(WARN, f"Профіль «{p['name']}» ({name}): зони ще не обведені на вашому записі",
                    "Запустіть pick_zones.bat (README, крок 5).")
        except UserError as e:
            say(FAIL, str(e))

    return finish()


def finish():
    print("=" * 70)
    if problems:
        print(f" Знайдено проблем: {problems}. Виправте пункти [ПОМИЛКА] за підказками вище.")
    else:
        print(" Усе необхідне встановлено. Пункти [УВАГА] — не критичні для поточного етапу.")
    print("=" * 70)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
