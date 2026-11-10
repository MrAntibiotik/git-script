"""Маленьке вікно з повідомленням (для кнопки DaVinci, якщо вбудовані вікна Resolve недоступні)."""

import sys
from pathlib import Path


def main():
    text = "?"
    if len(sys.argv) > 1:
        try:
            text = Path(sys.argv[1]).read_text(encoding="utf-8")
        except Exception as e:
            text = f"Не вдалося прочитати повідомлення: {e}"
    import tkinter as tk
    from tkinter import messagebox
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    messagebox.showinfo("Автонарізка шортсів", text, parent=root)
    root.destroy()


if __name__ == "__main__":
    main()
