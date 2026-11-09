"""Вибір рамок (зон) мишкою на кадрі-зразку з запису.

Запуск: перетягніть файл запису (або скриншот .png/.jpg) на pick_zones.bat.
  1. Оберіть профіль гри в консолі.
  2. Для відео — вкажіть секунду, на якій видно HUD (лічильники, лог, міцність, вебкамеру).
  3. У вікні: праворуч оберіть зону, ліворуч обведіть її мишкою. Після кожної рамки
     автоматично вибирається наступна зона.
  4. «Зберегти» — зони запишуться у профіль (config/profiles/<гра>.json),
     стара версія профілю збережеться поруч як .json.bak.

Режим без вікна: --preview малює всі зони на кадрі й зберігає PNG у output/.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image, ImageDraw, ImageFont  # noqa: E402

from shorts.config import (ask_profile, load_profile, load_profile_raw, load_settings,  # noqa: E402
                           output_dir, save_profile_raw, work_dir)
from shorts.errors import UserError  # noqa: E402
from shorts.util import parse_time  # noqa: E402

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".bmp", ".webp"}

# ключ: (назва, підказка, колір)
ZONE_INFO = {
    "webcam": ("Вебкамера", "Обведіть усю вебкамеру в кадрі гри (без рамки, якщо вона є). "
               "Ця частина піде нагору шортсу (V2).", "#ff4d4d"),
    "counters_block": ("Блок лічильників (V3)", "Увесь блок «шкода / заблоковано / допомога» разом "
                       "з іконками. Саме він накладається на гру в шортсі.", "#ffd84d"),
    "damage_counter": ("Число завданої шкоди", "ЛИШЕ перше число (завдана шкода), без іконки. Залиште "
                       "запас праворуч під 5 цифр (наприклад, «12 345»).", "#4dff88"),
    "event_log": ("Лог подій", "Область, де з'являються зелені й червоні рядки (напр. «612 Знищив …»). "
                  "Краще трохи більше, ніж менше.", "#4dc3ff"),
    "hp_text": ("Міцність — цифри", "Цифри міцності на панелі танка, наприклад «1650 / 2000».", "#ff9f4d"),
    "hp_bar": ("Міцність — смужка", "Лише сама кольорова смужка міцності, від лівого до правого краю "
               "повної смужки.", "#c44dff"),
    "end_screen": ("Екран смерті / кінця бою", "Область, де з'являються великі написи «Оцініть бій», "
                   "«Перемкнути камеру», «Перемога». Можна взяти з запасом.", "#ffffff"),
}
VISION_KEYS = ["damage_counter", "event_log", "hp_text", "hp_bar", "end_screen"]


def zone_keys_for(profile: dict) -> list[str]:
    keys = ["webcam"]
    if (profile.get("layout") or {}).get("counters", {}).get("enabled", False) or profile["signals"].get("vision"):
        keys.append("counters_block")
    if profile["signals"].get("vision"):
        keys += VISION_KEYS
    for k in (profile.get("zones") or {}):
        if k not in keys:
            keys.append(k)
    return keys


def load_font(size: int):
    for name in ("arial.ttf", "segoeui.ttf", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def draw_zones(img: Image.Image, zones: dict) -> Image.Image:
    out = img.convert("RGB").copy()
    d = ImageDraw.Draw(out)
    W, H = out.size
    font = load_font(max(14, H // 60))
    for key, z in zones.items():
        if not z:
            continue
        title, _, color = ZONE_INFO.get(key, (key, "", "#00ffff"))
        x0, y0 = z[0] * W, z[1] * H
        x1, y1 = x0 + z[2] * W, y0 + z[3] * H
        d.rectangle([x0, y0, x1, y1], outline=color, width=3)
        ty = y0 - font.size - 6 if y0 > font.size + 8 else y1 + 4
        d.text((x0 + 2, ty + 1), title, fill="black", font=font)
        d.text((x0 + 1, ty), title, fill=color, font=font)
    return out


def get_frame(settings: dict, src: Path, t: float | None) -> Image.Image:
    if src.suffix.lower() in IMAGE_EXT:
        return Image.open(src).convert("RGB")
    from shorts.media import extract_frame, probe
    info = probe(settings, src)
    if t is None:
        t = min(60.0, info.duration / 2)
    if t >= info.duration:
        raise UserError(f"Секунда {t:.0f} більша за тривалість запису ({info.duration:.0f} с).",
                        "Вкажіть менше значення.")
    png = extract_frame(settings, src, t, work_dir(settings) / "zone_frame.png")
    return Image.open(png).convert("RGB")


# ============================================================================ вікно

def run_gui(settings: dict, profile_id: str, src: Path, t: float | None):
    import tkinter as tk
    from tkinter import messagebox
    from PIL import ImageTk

    profile = load_profile(profile_id)
    zones: dict = {k: list(v) for k, v in (profile.get("zones") or {}).items()}
    keys = zone_keys_for(profile)
    state = {"img": get_frame(settings, src, t), "zoom": None, "sel": 0, "start": None, "tmp": None,
             "dirty": False}

    root = tk.Tk()
    root.title(f"Вибір зон — {profile['name']}")
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    root.geometry(f"{int(sw * 0.95)}x{int(sh * 0.88)}+10+10")

    left = tk.Frame(root)
    left.pack(side="left", fill="both", expand=True)
    right = tk.Frame(root, width=360, padx=8, pady=8)
    right.pack(side="right", fill="y")
    right.pack_propagate(False)

    canvas = tk.Canvas(left, bg="#202020", cursor="crosshair", highlightthickness=0)
    hbar = tk.Scrollbar(left, orient="horizontal", command=canvas.xview)
    vbar = tk.Scrollbar(left, orient="vertical", command=canvas.yview)
    canvas.configure(xscrollcommand=hbar.set, yscrollcommand=vbar.set)
    hbar.pack(side="bottom", fill="x")
    vbar.pack(side="right", fill="y")
    canvas.pack(side="left", fill="both", expand=True)

    tk.Label(right, text="Зони", font=("Segoe UI", 13, "bold")).pack(anchor="w")
    lb = tk.Listbox(right, height=len(keys) + 1, font=("Segoe UI", 11), exportselection=False,
                    activestyle="none")
    lb.pack(fill="x")
    hint = tk.Label(right, text="", wraplength=340, justify="left", font=("Segoe UI", 10), fg="#333")
    hint.pack(anchor="w", pady=(6, 10))
    coord = tk.Label(right, text="", font=("Consolas", 9), fg="#555")
    coord.pack(anchor="w")

    def btn(text, cmd, **kw):
        b = tk.Button(right, text=text, command=cmd, font=("Segoe UI", 10), **kw)
        b.pack(fill="x", pady=2)
        return b

    def refresh_list():
        lb.delete(0, "end")
        for k in keys:
            mark = "✔" if zones.get(k) else "  "
            lb.insert("end", f"{mark}  {ZONE_INFO.get(k, (k,))[0]}")
        for i, k in enumerate(keys):
            color = ZONE_INFO.get(k, ("", "", "#00aaaa"))[2]
            lb.itemconfig(i, fg="#000000" if color in ("#ffffff", "#ffd84d") else color)
        lb.selection_clear(0, "end")
        lb.selection_set(state["sel"])
        lb.see(state["sel"])
        k = keys[state["sel"]]
        hint.config(text=f"{ZONE_INFO.get(k, (k, ''))[0]}:\n{ZONE_INFO.get(k, (k, 'Обведіть зону.'))[1]}\n\n"
                         "Обведіть мишкою на кадрі ліворуч (затисніть ліву кнопку і тягніть).")

    def scale_fit() -> float:
        W, H = state["img"].size
        cw = max(200, canvas.winfo_width())
        ch = max(200, canvas.winfo_height())
        return min(cw / W, ch / H)

    def render():
        W, H = state["img"].size
        z = state["zoom"] or scale_fit()
        state["cur_scale"] = z
        disp = state["img"].resize((max(1, int(W * z)), max(1, int(H * z))), Image.LANCZOS)
        state["photo"] = ImageTk.PhotoImage(disp)
        canvas.delete("all")
        canvas.create_image(0, 0, image=state["photo"], anchor="nw", tags="img")
        canvas.configure(scrollregion=(0, 0, disp.width, disp.height))
        draw_rects()

    def draw_rects():
        canvas.delete("zone")
        W, H = state["img"].size
        z = state["cur_scale"]
        for i, k in enumerate(keys):
            r = zones.get(k)
            if not r:
                continue
            color = ZONE_INFO.get(k, ("", "", "#00ffff"))[2]
            x0, y0 = r[0] * W * z, r[1] * H * z
            x1, y1 = x0 + r[2] * W * z, y0 + r[3] * H * z
            width = 3 if i == state["sel"] else 2
            dash = None if i == state["sel"] else (4, 2)
            canvas.create_rectangle(x0, y0, x1, y1, outline=color, width=width, dash=dash, tags="zone")
            label = ZONE_INFO.get(k, (k,))[0]
            ty = y0 - 9 if y0 > 14 else y1 + 9
            canvas.create_text(x0 + 4, ty + 1, text=label, anchor="w", fill="black",
                               font=("Segoe UI", 9, "bold"), tags="zone")
            canvas.create_text(x0 + 3, ty, text=label, anchor="w", fill=color,
                               font=("Segoe UI", 9, "bold"), tags="zone")

    def to_frac(cx, cy):
        W, H = state["img"].size
        z = state["cur_scale"]
        return min(1.0, max(0.0, cx / (W * z))), min(1.0, max(0.0, cy / (H * z)))

    def on_press(ev):
        state["start"] = (canvas.canvasx(ev.x), canvas.canvasy(ev.y))
        color = ZONE_INFO.get(keys[state["sel"]], ("", "", "#00ffff"))[2]
        state["tmp"] = canvas.create_rectangle(*state["start"], *state["start"], outline=color, width=2)

    def on_drag(ev):
        on_move(ev)
        if state["start"] and state["tmp"]:
            canvas.coords(state["tmp"], *state["start"], canvas.canvasx(ev.x), canvas.canvasy(ev.y))

    def on_release(ev):
        if not state["start"]:
            return
        x0, y0 = state["start"]
        x1, y1 = canvas.canvasx(ev.x), canvas.canvasy(ev.y)
        state["start"] = None
        if state["tmp"]:
            canvas.delete(state["tmp"])
            state["tmp"] = None
        if abs(x1 - x0) < 4 or abs(y1 - y0) < 4:
            return
        fx0, fy0 = to_frac(min(x0, x1), min(y0, y1))
        fx1, fy1 = to_frac(max(x0, x1), max(y0, y1))
        zones[keys[state["sel"]]] = [round(fx0, 4), round(fy0, 4), round(fx1 - fx0, 4), round(fy1 - fy0, 4)]
        state["dirty"] = True
        # наступна зона без рамки, інакше просто наступна
        nxt = next((i for i in range(state["sel"] + 1, len(keys)) if not zones.get(keys[i])),
                   min(state["sel"] + 1, len(keys) - 1))
        state["sel"] = nxt
        refresh_list()
        draw_rects()

    def on_move(ev):
        W, H = state["img"].size
        fx, fy = to_frac(canvas.canvasx(ev.x), canvas.canvasy(ev.y))
        coord.config(text=f"x={fx * W:.0f}  y={fy * H:.0f}  ({fx:.3f}, {fy:.3f})")

    def on_select(_ev=None):
        cur = lb.curselection()
        if cur:
            state["sel"] = cur[0]
            refresh_list()
            draw_rects()

    def clear_zone():
        zones.pop(keys[state["sel"]], None)
        state["dirty"] = True
        refresh_list()
        draw_rects()

    def set_zoom(z):
        state["zoom"] = z
        render()

    def reload_frame():
        try:
            new_t = parse_time(t_entry.get())
            state["img"] = get_frame(settings, src, new_t)
            render()
        except (UserError, ValueError) as e:
            messagebox.showerror("Помилка", str(e))

    def save():
        raw = load_profile_raw(profile_id)
        raw.setdefault("zones", {})
        for k in list(raw["zones"].keys()):
            if k not in zones:
                del raw["zones"][k]
        for k, v in zones.items():
            raw["zones"][k] = v
        raw["zones_confirmed"] = True
        path = save_profile_raw(profile_id, raw)
        prev = output_dir(settings) / f"zones_{profile_id}.png"
        draw_zones(state["img"], zones).save(prev)
        state["dirty"] = False
        messagebox.showinfo("Збережено", f"Зони збережено у\n{path}\n\nКартинка для перевірки:\n{prev}")
        root.destroy()

    def cancel():
        if state["dirty"] and not messagebox.askyesno("Вийти", "Вийти без збереження змін?"):
            return
        root.destroy()

    lb.bind("<<ListboxSelect>>", on_select)
    canvas.bind("<ButtonPress-1>", on_press)
    canvas.bind("<B1-Motion>", on_drag)
    canvas.bind("<ButtonRelease-1>", on_release)
    canvas.bind("<Motion>", on_move)
    canvas.bind("<MouseWheel>", lambda e: canvas.yview_scroll(-1 if e.delta > 0 else 1, "units"))
    canvas.bind("<Shift-MouseWheel>", lambda e: canvas.xview_scroll(-1 if e.delta > 0 else 1, "units"))
    canvas.bind("<Configure>", lambda e: render() if state["zoom"] is None else None)
    root.bind("<Control-s>", lambda e: save())
    root.bind("<Escape>", lambda e: cancel())
    root.protocol("WM_DELETE_WINDOW", cancel)

    btn("Очистити вибрану зону", clear_zone)
    tk.Label(right, text="Масштаб (для дрібних зон — 100% або 200%):", font=("Segoe UI", 9)).pack(anchor="w", pady=(10, 0))
    zrow = tk.Frame(right)
    zrow.pack(fill="x")
    for text, z in (("Вмістити", None), ("100%", 1.0), ("200%", 2.0)):
        tk.Button(zrow, text=text, command=lambda z=z: set_zoom(z)).pack(side="left", expand=True, fill="x")

    if src.suffix.lower() not in IMAGE_EXT:
        tk.Label(right, text="Інший кадр — секунда запису (напр. 95 або 1:35):",
                 font=("Segoe UI", 9)).pack(anchor="w", pady=(10, 0))
        trow = tk.Frame(right)
        trow.pack(fill="x")
        t_entry = tk.Entry(trow, width=10)
        t_entry.insert(0, "" if t is None else str(t))
        t_entry.pack(side="left", fill="x", expand=True)
        tk.Button(trow, text="Показати", command=reload_frame).pack(side="left")

    tk.Label(right, text="").pack(pady=6)
    btn("Зберегти (Ctrl+S)", save, bg="#3a7d44", fg="white")
    btn("Вийти без збереження (Esc)", cancel)
    tk.Label(right, text="Порада: колесо миші — прокрутка, Shift+колесо — вбік.",
             font=("Segoe UI", 8), fg="#666", wraplength=340, justify="left").pack(anchor="w", pady=(8, 0))

    refresh_list()
    root.after(100, render)
    root.mainloop()


def main():
    ap = argparse.ArgumentParser(description="Вибір зон (рамок) на кадрі-зразку")
    ap.add_argument("source", nargs="?", help="файл запису або скриншот")
    ap.add_argument("--profile", help="профіль гри (wot, pubg, …)")
    ap.add_argument("--time", help="секунда кадру (напр. 95 або 1:35)")
    ap.add_argument("--preview", action="store_true", help="лише зберегти картинку з поточними зонами")
    args = ap.parse_args()

    settings = load_settings()
    src = args.source
    if not src:
        src = input("Перетягніть сюди файл запису або скриншот і натисніть Enter: ").strip().strip('"')
    src = Path(src)
    if not src.exists():
        raise UserError(f"Файл не знайдено: {src}", "Перетягніть файл на pick_zones.bat.")
    profile_id = args.profile or ask_profile("wot")
    t = parse_time(args.time) if args.time else None
    if t is None and src.suffix.lower() not in IMAGE_EXT and not args.preview:
        ans = input("На якій секунді запису добре видно HUD? (напр. 95 або 1:35) [Enter = 60]: ").strip()
        t = parse_time(ans) if ans else None

    if args.preview:
        prof = load_profile(profile_id)
        out = output_dir(settings) / f"zones_{profile_id}.png"
        draw_zones(get_frame(settings, src, t), prof.get("zones") or {}).save(out)
        print(f"Збережено: {out}")
        return
    run_gui(settings, profile_id, src, t)


if __name__ == "__main__":
    try:
        main()
    except UserError as e:
        print(f"\n[ПОМИЛКА] {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(1)
