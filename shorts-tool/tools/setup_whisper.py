"""Встановлення whisper.cpp (з підтримкою NVIDIA CUDA) і моделі розпізнавання мови.

Запуск: setup_whisper.bat. Що робить:
  1. Знаходить останню збірку whisper.cpp для Windows з CUDA на GitHub (ggml-org/whisper.cpp)
     і розпаковує її в папку whisper\\.
  2. Завантажує модель (за замовчуванням large-v3-turbo, ~1,6 ГБ) у models\\whisper\\.
     Якщо завантаження обірветься — запустіть ще раз, воно продовжиться.
  3. Робить пробне розпізнавання на відеокарті. Якщо відеокарта не спрацювала —
     перевіряє процесор і, якщо треба, сам вимикає GPU в settings.json.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402

from shorts.config import ROOT, SETTINGS_FILE, load_settings, resolve_path  # noqa: E402

API = "https://api.github.com/repos/ggml-org/whisper.cpp/releases"
MODELS = {
    "1": ("ggml-large-v3-turbo.bin", "найкраща якість/швидкість (~1,6 ГБ) — рекомендовано"),
    "2": ("ggml-medium.bin", "середня (~1,5 ГБ), трохи гірша якість"),
    "3": ("ggml-small.bin", "мала (~0,5 ГБ), швидка, але помилок більше"),
}
HF = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/"
UA = {"User-Agent": "shorts-tool-setup"}


def http_json(url: str):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def download(url: str, dst: Path):
    """Завантаження з прогресом і докачуванням."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    part = dst.with_suffix(dst.suffix + ".part")
    have = part.stat().st_size if part.exists() else 0
    headers = dict(UA)
    if have:
        headers["Range"] = f"bytes={have}-"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=60) as r:
        total = int(r.headers.get("Content-Length") or 0) + have
        if have and r.status != 206:
            have = 0
            part.unlink(missing_ok=True)
        mode = "ab" if have else "wb"
        done = have
        t0 = time.time()
        with open(part, mode) as f:
            while True:
                buf = r.read(1 << 20)
                if not buf:
                    break
                f.write(buf)
                done += len(buf)
                if total:
                    speed = (done - have) / max(1e-3, time.time() - t0) / 1e6
                    print(f"\r  {done / 1e6:7.0f} / {total / 1e6:.0f} МБ  ({done * 100 / total:4.1f} %, {speed:.1f} МБ/с)   ",
                          end="", flush=True)
    print()
    part.replace(dst)


def cuda_version(name: str) -> tuple[int, ...]:
    m = re.search(r"(?:cuda|cublas)-(\d+)\.(\d+)(?:\.(\d+))?", name)
    return tuple(int(x) for x in m.groups() if x) if m else (0,)


def install_binary() -> Path | None:
    print("\n[1/3] Шукаю збірку whisper.cpp для Windows з CUDA…")
    try:
        releases = http_json(API + "?per_page=10")
    except Exception as e:
        print(f"[ПОМИЛКА] Не вдалося звернутися до GitHub: {e}")
        return None
    for rel in releases:
        assets = rel.get("assets", [])
        # назви бувають «whisper-bin-win-cuda-12.4.0-x64.zip» (нові) або «whisper-cublas-12.4.0-bin-x64.zip» (старі)
        cuda = [a for a in assets if ("cuda" in a["name"] or "cublas" in a["name"]) and "x64" in a["name"]
                and "arm64" not in a["name"] and a["name"].endswith(".zip")]
        cpu = [a for a in assets if re.fullmatch(r"whisper-bin-x64\.zip", a["name"])]
        if not cuda and not cpu:
            continue
        cuda.sort(key=lambda a: cuda_version(a["name"]), reverse=True)
        asset = (cuda or cpu)[0]
        print(f"  Версія {rel['tag_name']}: {asset['name']} ({asset['size'] / 1e6:.0f} МБ)")
        zdst = ROOT / "whisper" / asset["name"]
        if not zdst.exists():
            download(asset["browser_download_url"], zdst)
        target = ROOT / "whisper" / zdst.stem
        if target.exists():
            shutil.rmtree(target)
        with zipfile.ZipFile(zdst) as z:
            z.extractall(target)
        exe = sorted(target.rglob("whisper-cli.exe"))
        if exe:
            print(f"  Встановлено: {exe[0]}")
            return exe[0]
        print("[ПОМИЛКА] У архіві немає whisper-cli.exe")
        return None
    print("[ПОМИЛКА] Не знайдено збірки для Windows у релізах.")
    return None


def install_model(settings: dict) -> Path:
    print("\n[2/3] Модель розпізнавання мови:")
    for k, (n, d) in MODELS.items():
        print(f"  {k}. {n} — {d}")
    if "--auto" in sys.argv:          # автоматичне встановлення (SETUP.bat) — рекомендована модель
        ans = "1"
        print("  Обрано 1 (рекомендовано).")
    else:
        ans = input("Номер [Enter = 1]: ").strip() or "1"
    name = MODELS.get(ans, MODELS["1"])[0]
    dst = ROOT / "models" / "whisper" / name
    if dst.exists() and dst.stat().st_size > 100e6:
        print(f"  Уже є: {dst}")
    else:
        print(f"  Завантажую {name}…")
        download(HF + name, dst)
    rel = f"models/whisper/{name}"
    if settings["whisper"]["model"] != rel:
        update_settings({"model": rel})
    return dst


def update_settings(changes: dict):
    data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8-sig"))
    data.setdefault("whisper", {}).update(changes)
    SETTINGS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"  settings.json оновлено: {changes}")


def test_run(exe: Path, model: Path) -> None:
    import soundfile as sf
    print("\n[3/3] Пробне розпізнавання…")
    tmp = ROOT / "work" / "whisper_test"
    tmp.mkdir(parents=True, exist_ok=True)
    wav = tmp / "test.wav"
    t = np.arange(16000 * 3) / 16000
    sf.write(str(wav), (0.1 * np.sin(2 * np.pi * 220 * t)).astype(np.float32), 16000, subtype="PCM_16")
    for gpu in (True, False):
        cmd = [str(exe), "-m", str(model), "-f", str(wav), "-l", "uk"] + ([] if gpu else ["-ng"])
        t0 = time.time()
        res = subprocess.run(cmd, capture_output=True, cwd=str(exe.parent))
        err = res.stderr.decode("utf-8", "replace")
        if res.returncode == 0:
            dev = "відеокарті" if gpu else "процесорі"
            used_cuda = "CUDA" in err or "cuda" in err.lower()
            print(f"[ OK ] whisper.cpp працює на {dev} ({time.time() - t0:.1f} с){'' if not gpu or used_cuda else ' (CUDA в лозі не видно)'}")
            if not gpu:
                print("[УВАГА] На відеокарті не запрацював — субтитри рахуватимуться процесором (повільніше).")
                update_settings({"gpu": False})
            else:
                update_settings({"gpu": True})
            return
        print(f"  {'GPU' if gpu else 'CPU'}: помилка (код {res.returncode}): {err.strip()[-400:]}")
    print("[ПОМИЛКА] whisper.cpp не запускається. Надішліть текст вище розробнику.")


def main():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    print("=" * 70)
    print(" Встановлення whisper.cpp і моделі для субтитрів")
    print("=" * 70)
    settings = load_settings()
    exe = install_binary()
    if exe is None:
        existing = sorted((ROOT / "whisper").rglob("whisper-cli.exe"))
        exe = existing[0] if existing else None
    model = install_model(settings)
    if exe:
        test_run(exe, model)
    else:
        print("\nВстановіть whisper.cpp вручну: https://github.com/ggml-org/whisper.cpp/releases — архів "
              "whisper-cublas-…-bin-x64.zip розпакуйте в папку whisper.")
    print("\nГотово.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nПерервано. Запустіть ще раз — завантаження продовжиться.")
