"""Оновлення профілів ігор при SETUP.bat: нова версія профілю + ВАШІ рамки, категорії звуків.

Запуск: python merge_profiles.py <нові_профілі> <ваші_профілі>
Якщо у новому профілі profile_version більший, ніж у вашому — беремо новий,
але переносимо з вашого: zones, zones_confirmed і власні категорії game_audio.
Стара версія зберігається поруч як .json.bak. Лише стандартна бібліотека Python.
"""

import json
import shutil
import sys
from pathlib import Path


def main():
    src, dst = Path(sys.argv[1]), Path(sys.argv[2])
    dst.mkdir(parents=True, exist_ok=True)
    for new_p in sorted(src.glob("*.json")):
        old_p = dst / new_p.name
        if not old_p.exists():
            shutil.copy2(new_p, old_p)
            print(f"  + профіль {new_p.stem}")
            continue
        new = json.loads(new_p.read_text(encoding="utf-8-sig"))
        try:
            old = json.loads(old_p.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError:
            print(f"  [УВАГА] {old_p.name} пошкоджений — не чіпаю")
            continue
        if int(new.get("profile_version", 1)) <= int(old.get("profile_version", 1)):
            continue
        for key in ("zones", "zones_confirmed"):
            if key in old:
                new[key] = old[key]
        old_cats = (old.get("game_audio") or {}).get("categories") or {}
        new_cats = new.setdefault("game_audio", {}).setdefault("categories", {})
        for k, v in old_cats.items():
            new_cats.setdefault(k, v)
        shutil.copy2(old_p, old_p.with_suffix(".json.bak"))
        old_p.write_text(json.dumps(new, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"  * профіль {new_p.stem} оновлено до версії {new.get('profile_version')} (ваші рамки збережено)")


if __name__ == "__main__":
    main()
