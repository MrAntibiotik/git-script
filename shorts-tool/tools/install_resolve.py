"""Встановлення кнопок у меню DaVinci Resolve: Workspace → Scripts → Shorts Autocut / Shorts Reimport Last.

Кладе два маленькі Lua-файли в папку скриптів Resolve поточного користувача:
  %APPDATA%\\Blackmagic Design\\DaVinci Resolve\\Support\\Fusion\\Scripts\\Utility\\
Уся логіка лишається в папці інструмента (resolve\\shorts_lib.lua), тому після оновлення
інструмента перевстановлювати кнопки не треба. Після встановлення ПЕРЕЗАПУСТІТЬ DaVinci Resolve.
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

STUB = """-- Автонарізка шортсів: {title}
-- Уся логіка — у файлі {lib}
local TOOL_DIR = [[{root}]]
local ok, err = pcall(function()
  local lib = dofile(TOOL_DIR .. [[\\resolve\\shorts_lib.lua]])
  lib.main(TOOL_DIR, "{mode}")
end)
if not ok then
  print("[Шортси] Помилка: " .. tostring(err))
end
"""


def main():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    root = str(ROOT)
    if not root.isascii() or " " in root:
        print(f"[УВАГА] Шлях до інструмента містить кирилицю або пробіли: {root}\n"
              "        DaVinci може не відкрити файли. Рекомендовано перенести папку в C:\\ShortsTool.")
    appdata = os.environ.get("APPDATA")
    if not appdata:
        print("[ПОМИЛКА] Не знайдено змінну APPDATA — це точно Windows?")
        return 1
    target = Path(appdata) / "Blackmagic Design" / "DaVinci Resolve" / "Support" / "Fusion" / "Scripts" / "Utility"
    target.mkdir(parents=True, exist_ok=True)
    lib = ROOT / "resolve" / "shorts_lib.lua"
    for fname, title, mode in (("Shorts Autocut.lua", "запуск / імпорт результату", "auto"),
                               ("Shorts Reimport Last.lua", "зібрати останній результат ще раз", "reimport")):
        p = target / fname
        p.write_text(STUB.format(title=title, lib=lib, root=root, mode=mode), encoding="utf-8")
        print(f"[ OK ] {p}")
    print("\nГотово. ПЕРЕЗАПУСТІТЬ DaVinci Resolve, потім: Workspace → Scripts → Shorts Autocut.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
