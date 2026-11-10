"""Дані для DaVinci: файл-результат у вигляді Lua-таблиці, маркери, EDL, статус завдання.

Lua-скрипт у DaVinci не вміє читати JSON, тому Python пише результат одразу як
Lua-код (`return { ... }`), який Lua завантажує через dofile(). Шляхи до службових
файлів — лише латиницею (Lua у Windows не відкриває файли з кирилицею в шляху).
"""

from __future__ import annotations

import json
import math
import re
import time
from pathlib import Path

from . import events as E
from .config import work_dir
from .events import Event
from .util import fmt_time

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
LUA_KEYWORDS = {"and", "break", "do", "else", "elseif", "end", "false", "for", "function", "if", "in",
                "local", "nil", "not", "or", "repeat", "return", "then", "true", "until", "while", "goto"}


def lua_str(s: str) -> str:
    out = ['"']
    for ch in s:
        o = ord(ch)
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif o < 32 or o == 127:
            out.append(f"\\{o:03d}")
        else:
            out.append(ch)   # UTF-8 як є — Lua зберігає рядки байтами
    out.append('"')
    return "".join(out)


def to_lua(v, ind: int = 0) -> str:
    pad = "  " * (ind + 1)
    end = "  " * ind
    if v is None:
        return "nil"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int,)) and not isinstance(v, bool):
        return str(v)
    if isinstance(v, float):
        v = float(v)   # numpy.float64 -> звичайне число (інакше repr дає «np.float64(...)»)
        if not math.isfinite(v):
            return "0"
        return repr(round(v, 6))
    if isinstance(v, (str, Path)):
        return lua_str(str(v))
    if isinstance(v, (list, tuple)):
        if not v:
            return "{}"
        if all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in v):
            return "{" + ", ".join(to_lua(x) for x in v) + "}"
        return "{\n" + ",\n".join(pad + to_lua(x, ind + 1) for x in v) + "\n" + end + "}"
    if isinstance(v, dict):
        if not v:
            return "{}"
        items = []
        for k, x in v.items():
            if x is None:
                continue
            key = k if (isinstance(k, str) and _IDENT.match(k) and k not in LUA_KEYWORDS) else f"[{to_lua(k)}]"
            items.append(f"{pad}{key} = {to_lua(x, ind + 1)}")
        return "{\n" + ",\n".join(items) + "\n" + end + "}"
    try:   # numpy-числа
        return to_lua(v.item(), ind)
    except AttributeError:
        return lua_str(str(v))


def write_lua(path: Path, data) -> Path:
    tmp = path.with_suffix(".tmp")
    tmp.write_text("-- Згенеровано інструментом автонарізки шортсів. Не редагуйте вручну.\nreturn "
                   + to_lua(data) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


# ------------------------------------------------------------------ маркери

def marker_name(e: Event) -> str:
    if e.type == E.HIT:
        return f"Влучання +{int(e.value)}" if e.value else "Влучання"
    if e.type == E.DAMAGE_TAKEN:
        return f"Отримав шкоду {int(e.value)}" if e.value else "Отримав шкоду"
    if e.type == E.KILL:
        return "Знищив ворога"
    if e.type == E.DEATH:
        return "Мене знищено"
    if e.type == E.VOICE:
        return f"Голос: {e.extra.get('kind', '')}"
    if e.type == E.GAME_SOUND:
        return f"Звук: {e.extra.get('category', '')}"
    return e.label


def markers_for(events: list[Event], colors: dict, min_conf: float = 0.0) -> list[dict]:
    out = []
    for e in sorted(events, key=lambda e: e.t):
        if e.type not in colors or e.confidence < min_conf:
            continue
        note = f"score {e.extra.get('score', 0)} | {e.source} | {e.detail}"
        if e.extra.get("why"):
            note += f" | {e.extra['why']}"
        out.append({"t": round(e.t, 3), "color": colors[e.type], "name": marker_name(e), "note": note[:240],
                    "type": e.type})
    return out


def _tc(frames: int, fps: int) -> str:
    f = frames % fps
    s = frames // fps
    return f"{s // 3600:02d}:{(s // 60) % 60:02d}:{s % 60:02d}:{f:02d}"


def write_marker_edl(path: Path, title: str, markers: list[dict], fps: float, start_tc_hours: int = 1) -> Path:
    """EDL у форматі DaVinci «Timeline Markers» (імпорт: ПКМ по таймлайну → Timelines → Import → Timeline Markers from EDL)."""
    ifps = int(round(fps))
    base = start_tc_hours * 3600 * ifps
    lines = [f"TITLE: {title}", "FCM: NON-DROP FRAME", ""]
    used = set()
    for i, m in enumerate(markers, 1):
        fr = base + int(round(m["t"] * fps))
        while fr in used:
            fr += 1
        used.add(fr)
        a, b = _tc(fr, ifps), _tc(fr + 1, ifps)
        lines.append(f"{i:03d}  001      V     C        {a} {b} {a} {b}  ")
        name = m["name"].replace("|", "/")
        lines.append(f" |C:ResolveColor{m['color']} |M:{name} |D:1")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


# ------------------------------------------------------------------ статус завдання (для Lua-кнопки)

def jobs_dir(settings: dict) -> Path:
    d = work_dir(settings) / "jobs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def write_status(settings: dict, **fields) -> None:
    d = jobs_dir(settings)
    cur = read_status(settings) or {}
    cur.update(fields)
    cur["heartbeat"] = int(time.time())
    (d / "latest.json").write_text(json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8")
    write_lua(d / "latest.lua", cur)


def read_status(settings: dict) -> dict | None:
    p = jobs_dir(settings) / "latest.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def describe_event(e: Event) -> str:
    return f"{fmt_time(e.t)} {marker_name(e)}"
