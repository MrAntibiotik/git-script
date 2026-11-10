"""Зоровий детектор (основний для World of Tanks).

Працює у два кроки:
  1) СКАНУВАННЯ (повільно, один раз): ffmpeg дає кадри 2 рази/с, з кожного вирізаються
     малі зони, розпізнається текст. Результат («показання») пишеться у work/... частинами
     по 10 хв. Якщо обробку перервати — наступний запуск продовжить з місця зупинки.
  2) АНАЛІЗ (миттєво): з показань отримуємо події — влучання, отримана шкода, знищення
     ворога, моя смерть, початок/кінець бою. Пороги можна міняти в профілі й
     переаналізувати без повторного сканування (параметр --reanalyze).

Захист від помилок розпізнавання:
  - лічильник шкоди лише зростає; нове значення приймається, коли воно однакове
    на кількох кадрах поспіль; неправдоподібний стрибок (> max_jump) відкидається;
  - обнулення лічильника (новий бій) приймається лише після 4 однакових кадрів;
  - колір рядка логу визначається за пікселями (зелений/червоний), не через OCR.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import time
from pathlib import Path

import cv2
import numpy as np

from . import events as E
from .config import VISION_ZONES, work_dir, zone_px
from .errors import UserError
from .events import Event
from .media import VideoInfo, iter_frames
from .ocr import DIGITS, HP_CHARS, OcrEngine, group_lines
from .util import contains_keyword, first_number, fmt_time, partial_ratio, normalize_words

log = logging.getLogger("vision")

_HP_RE = re.compile(r"(\d{1,5})\s*/\s*(\d{2,5})")


# =====================================================================================
#  Допоміжне: кольори, смужка міцності, зміни в зоні
# =====================================================================================

def classify_color(img_bgr: np.ndarray) -> str:
    """Домінантний колір тексту: 'green' / 'red' / 'yellow' / 'white'."""
    if img_bgr is None or img_bgr.size == 0:
        return "white"
    hsv = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2HSV)
    h, s, v = hsv[..., 0], hsv[..., 1], hsv[..., 2]
    mask = (s > 90) & (v > 110)
    n = int(mask.sum())
    if n < 12:
        return "white"
    hues = h[mask]
    green = int(((hues >= 35) & (hues <= 90)).sum())
    red = int(((hues <= 12) | (hues >= 165)).sum())
    yellow = int(((hues > 12) & (hues < 35)).sum())
    best = max((green, "green"), (red, "red"), (yellow, "yellow"))
    if best[0] < 0.4 * n:
        return "white"
    return best[1]


def bar_fill(img_bgr: np.ndarray) -> float | None:
    """Частка заповнення горизонтальної смужки (0..1) за насиченими колонками."""
    if img_bgr is None or img_bgr.size == 0:
        return None
    hsv = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2HSV)
    colored = (hsv[..., 1] > 80) & (hsv[..., 2] > 90)
    col_frac = colored.mean(axis=0)
    filled = np.where(col_frac > 0.4)[0]
    if filled.size == 0:
        return 0.0
    # Смужка зліва направо: шукаємо правий край суцільної заповненої частини.
    return float(filled.max() + 1) / img_bgr.shape[1]


class ChangeGate:
    """Чи змінилася зона з минулого розпізнавання (щоб не запускати OCR даремно)."""

    def __init__(self, pixel_delta: int, fraction: float, refresh_s: float):
        self.pixel_delta = pixel_delta
        self.fraction = fraction
        self.refresh_s = refresh_s
        self.prev: np.ndarray | None = None
        self.prev_t = -1e9

    def changed(self, img: np.ndarray, t: float) -> bool:
        g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        if g.shape[1] > 320:
            k = 320 / g.shape[1]
            g = cv2.resize(g, None, fx=k, fy=k, interpolation=cv2.INTER_AREA)
        if self.prev is None or self.prev.shape != g.shape or t - self.prev_t >= self.refresh_s:
            self.prev, self.prev_t = g, t
            return True
        diff = cv2.absdiff(g, self.prev)
        frac = float((diff > self.pixel_delta).mean())
        if frac >= self.fraction:
            self.prev, self.prev_t = g, t
            return True
        return False


# =====================================================================================
#  КРОК 1. Сканування відео -> показання
# =====================================================================================

def _cache_dir(settings: dict, info: VideoInfo, profile: dict) -> Path:
    zones = {k: profile["zones"].get(k) for k in VISION_ZONES}
    key = json.dumps({"z": zones, "fps": profile["vision"]["sample_fps"],
                      "ocr": settings["ocr"], "v": 2}, sort_keys=True)
    h = hashlib.md5(key.encode()).hexdigest()[:8]
    st = info.path.stat()
    vid = hashlib.md5(f"{info.path.name}|{st.st_size}".encode()).hexdigest()[:6]
    safe = re.sub(r"[^\w\-]+", "_", info.path.stem)[:60]
    d = work_dir(settings) / f"{safe}_{vid}" / f"vision_{profile['id']}_{h}"
    d.mkdir(parents=True, exist_ok=True)
    return d


class VisionScanner:
    def __init__(self, settings: dict, profile: dict, info: VideoInfo, debug_dir: Path | None = None):
        self.settings = settings
        self.profile = profile
        self.vcfg = profile["vision"]
        self.info = info
        self.fps = float(self.vcfg["sample_fps"])
        self.ocr: OcrEngine | None = None
        self.debug_dir = debug_dir
        self.debug_count = 0
        ocfg = settings["ocr"]

        zones = profile.get("zones") or {}
        self.zones_px: dict[str, tuple[int, int, int, int]] = {}
        enabled = {
            "damage_counter": self.vcfg["damage_counter"]["enabled"],
            "event_log": self.vcfg["event_log"]["enabled"],
            "hp_text": self.vcfg["hp"]["enabled"] and self.vcfg["hp"]["use_text"],
            "hp_bar": self.vcfg["hp"]["enabled"] and self.vcfg["hp"]["use_bar"],
            "end_screen": self.vcfg["end_screen"]["enabled"],
        }
        for k in VISION_ZONES:
            if enabled.get(k) and zones.get(k):
                self.zones_px[k] = zone_px(zones[k], info.width, info.height)
        if not self.zones_px:
            raise UserError(f"У профілі «{profile['id']}» не задано жодної зони для розпізнавання.",
                            "Запустіть pick_zones.bat і обведіть зони на кадрі з запису.")

        # Загальний прямокутник усіх зон — ffmpeg віддає лише його.
        xs0 = [z[0] for z in self.zones_px.values()]
        ys0 = [z[1] for z in self.zones_px.values()]
        xs1 = [z[0] + z[2] for z in self.zones_px.values()]
        ys1 = [z[1] + z[3] for z in self.zones_px.values()]
        bx, by = min(xs0) & ~1, min(ys0) & ~1
        bw = min(info.width - bx, max(xs1) - bx + 1)
        bh = min(info.height - by, max(ys1) - by + 1)
        self.bbox = (bx, by, bw - bw % 2, bh - bh % 2)

        self.gates = {k: ChangeGate(ocfg["change_pixel_delta"], ocfg["change_fraction"], ocfg["refresh_s"])
                      for k in self.zones_px}
        self.last: dict[str, object] = {}
        self.force: set[str] = set()   # зони, які треба перечитати на наступному кадрі
        self.last_end_t = -1e9

    # ---------------------------------------------------------------- зони

    def _crop(self, frame: np.ndarray, key: str) -> np.ndarray:
        x, y, w, h = self.zones_px[key]
        bx, by = self.bbox[0], self.bbox[1]
        return frame[y - by:y - by + h, x - bx:x - bx + w]

    def _dbg(self, t: float, key: str, img: np.ndarray, text: str):
        if not self.debug_dir or self.debug_count > 3000:
            return
        self.debug_dir.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^\w\-]+", "_", text)[:40]
        name = f"{int(t // 60):04d}m{t % 60:05.2f}s_{key}_{safe}.png"
        ok, buf = cv2.imencode(".png", img)
        if ok:
            (self.debug_dir / name).write_bytes(buf.tobytes())
            self.debug_count += 1

    def scan_frame(self, t: float, frame: np.ndarray) -> dict:
        ocfg = self.settings["ocr"]
        r: dict = {"t": round(t, 3)}

        if "damage_counter" in self.zones_px:
            img = self._crop(frame, "damage_counter")
            if self.gates["damage_counter"].changed(img, t) or "dmg" in self.force:
                self.force.discard("dmg")
                text = self.ocr.read_line(img, allowlist=DIGITS, scale=ocfg["digit_upscale"])
                if text != self.last.get("dmg_raw"):
                    # Нове значення підтверджуємо НЕЗАЛЕЖНИМ читанням наступного кадру.
                    self.force.add("dmg")
                    self._dbg(t, "dmg", img, text)
                self.last["dmg_raw"] = text
            r["dmg_raw"] = self.last.get("dmg_raw", "")

        if "hp_text" in self.zones_px:
            img = self._crop(frame, "hp_text")
            if self.gates["hp_text"].changed(img, t) or "hp" in self.force:
                self.force.discard("hp")
                text = self.ocr.read_line(img, allowlist=HP_CHARS, scale=ocfg["digit_upscale"])
                if text != self.last.get("hp_raw"):
                    self.force.add("hp")
                    self._dbg(t, "hp", img, text)
                self.last["hp_raw"] = text
            r["hp_raw"] = self.last.get("hp_raw", "")

        if "hp_bar" in self.zones_px:
            f = bar_fill(self._crop(frame, "hp_bar"))
            r["hp_bar"] = None if f is None else round(f, 3)

        if "event_log" in self.zones_px:
            img = self._crop(frame, "event_log")
            if self.gates["event_log"].changed(img, t):
                boxes = self.ocr.read_boxes(img, scale=ocfg["text_upscale"])
                lines = []
                for line in group_lines(boxes):
                    x0 = min(b.x0 for b in line); y0 = min(b.y0 for b in line)
                    x1 = max(b.x1 for b in line); y1 = max(b.y1 for b in line)
                    text = " ".join(b.text for b in line)
                    color = classify_color(img[max(0, y0):y1, max(0, x0):x1])
                    conf = sum(b.conf for b in line) / len(line)
                    lines.append({"text": text, "color": color, "conf": round(conf, 2), "y": y0})
                self.last["log"] = lines
                sig = "|".join(f"{l['color'][0]}:{l['text']}" for l in lines)
                if sig != self.last.get("log_sig"):
                    self._dbg(t, "log", img, sig[:40])
                self.last["log_sig"] = sig
            r["log"] = self.last.get("log", [])

        if "end_screen" in self.zones_px:
            ecfg = self.vcfg["end_screen"]
            if t - self.last_end_t >= ecfg["check_every_s"]:
                self.last_end_t = t
                img = self._crop(frame, "end_screen")
                if self.gates["end_screen"].changed(img, t):
                    boxes = self.ocr.read_boxes(img, scale=ecfg["scale"])
                    text = " ".join(b.text for b in sorted(boxes, key=lambda b: (b.y0 // 20, b.x0)))
                    if text and contains_keyword(text, ecfg["death_keywords"] + ecfg["battle_end_keywords"],
                                                 ecfg["keyword_ratio"]):
                        self._dbg(t, "end", img, text)
                    self.last["end"] = text
                r["end"] = self.last.get("end", "")
        return r

    # ---------------------------------------------------------------- увесь файл

    def scan(self, start: float = 0.0, end: float | None = None, progress=None) -> list[dict]:
        """Повертає всі показання на відрізку. Частини кешуються у work/."""
        end = self.info.duration if end is None else min(end, self.info.duration)
        if end <= start:
            raise UserError("Кінець відрізка раніше за початок.", "Перевірте --start/--end.")
        chunk = max(60.0, float(self.settings["processing"]["chunk_minutes"]) * 60)
        cache = _cache_dir(self.settings, self.info, self.profile)
        readings: list[dict] = []
        # Частини вирівняні по сітці 0, 10 хв, 20 хв… — кеш перевикористовується між запусками.
        k0, k1 = int(start // chunk), int(math.ceil(end / chunk))
        n_chunks = k1 - k0
        t_begin = time.time()
        for i, k in enumerate(range(k0, k1)):
            c0 = max(start, k * chunk)
            c1 = min(end, (k + 1) * chunk)
            if c1 - c0 < 0.01:
                continue
            f = cache / f"chunk_{c0:09.1f}_{c1:09.1f}.jsonl"
            if f.exists():
                readings.extend(json.loads(l) for l in f.read_text(encoding="utf-8").splitlines() if l)
                log.info("  частина %d/%d (%s–%s): з кешу", i + 1, n_chunks, fmt_time(c0, 0), fmt_time(c1, 0))
                continue
            if self.ocr is None:
                self.ocr = OcrEngine(self.settings)
            part: list[dict] = []
            t0 = time.time()
            for t, frame in iter_frames(self.settings, self.info, c0, c1 - c0, self.fps, crop=self.bbox):
                if t >= c1:
                    break
                part.append(self.scan_frame(t, frame))
                if progress and len(part) % 20 == 0:
                    progress((t - start) / (end - start))
            tmp = f.with_suffix(".tmp")
            tmp.write_text("\n".join(json.dumps(p, ensure_ascii=False) for p in part) + "\n", encoding="utf-8")
            tmp.replace(f)
            readings.extend(part)
            dt = time.time() - t0
            speed = (c1 - c0) / dt if dt > 0 else 0
            log.info("  частина %d/%d (%s–%s): %d кадрів, %.0f с, швидкість ×%.1f від реального часу",
                     i + 1, n_chunks, fmt_time(c0, 0), fmt_time(c1, 0), len(part), dt, speed)
        log.info("Сканування завершено за %.0f с. Показань: %d. Кеш: %s",
                 time.time() - t_begin, len(readings), cache)
        if progress:
            progress(1.0)
        return readings


# =====================================================================================
#  КРОК 2. Аналіз показань -> події
# =====================================================================================

class DamageTracker:
    """Лічильник завданої шкоди: кожне підтверджене зростання = влучання."""

    def __init__(self, cfg: dict, tcorr: float):
        self.cfg = cfg
        self.tcorr = tcorr
        self.value: int | None = None
        self.cand: int | None = None
        self.cand_n = 0
        self.change_t: float | None = None

    def update(self, t: float, raw: str) -> list[Event]:
        v = first_number(raw or "")
        if v is None or v > self.cfg["max_value"]:
            return []
        if self.value is None:
            return self._confirm(t, v, self.cfg["confirm_frames"], init=True)
        if v == self.value:
            self.cand, self.cand_n, self.change_t = None, 0, None
            return []
        if self.change_t is None:
            self.change_t = t
        diff = v - self.value
        if 0 < diff <= self.cfg["max_jump"]:
            need = self.cfg["confirm_frames"]
        elif diff < 0 and (v == 0 or v < self.value * 0.5):
            need = self.cfg["reset_confirm_frames"]
        else:  # неправдоподібний стрибок або «зменшення» — найімовірніше помилка OCR
            need = self.cfg["resync_frames"] * (2 if diff < 0 else 1)
        return self._confirm(t, v, need)

    def _confirm(self, t: float, v: int, need: int, init: bool = False) -> list[Event]:
        if v == self.cand:
            self.cand_n += 1
        else:
            self.cand, self.cand_n = v, 1
            if init:
                self.change_t = t
        if self.cand_n < need:
            return []
        ev_t = (self.change_t if self.change_t is not None else t) + self.tcorr
        old = self.value
        self.value = v
        self.cand, self.cand_n, self.change_t = None, 0, None
        if old is None:
            log.debug("%s лічильник шкоди: стартове значення %d", fmt_time(ev_t), v)
            return []
        diff = v - old
        if 0 < diff <= self.cfg["max_jump"]:
            return [Event(ev_t, E.HIT, "vision:лічильник", value=diff, confidence=1.0,
                          detail=f"{old} → {v}", extra={"counter": v})]
        if diff < 0 and (v == 0 or v < old * 0.5):
            out = [Event(ev_t, E.BATTLE_START, "vision:лічильник", value=v, detail=f"лічильник {old} → {v}")]
            if v > 0:
                out.append(Event(ev_t, E.HIT, "vision:лічильник", value=v, confidence=0.6,
                                 detail=f"0 → {v} (перше влучання)", extra={"counter": v}))
            return out
        if diff > 0:
            return [Event(ev_t, E.HIT, "vision:лічильник", value=diff, confidence=0.5,
                          detail=f"{old} → {v} (великий стрибок, перевірте)", extra={"counter": v})]
        log.debug("%s лічильник: прийнято корекцію %d → %d", fmt_time(ev_t), old, v)
        return []


class HpTracker:
    """Міцність танка: текст «1234 / 2000» (основне) і смужка (запасне)."""

    def __init__(self, cfg: dict, tcorr: float):
        self.cfg = cfg
        self.tcorr = tcorr
        self.cur: int | None = None
        self.max: int | None = None
        self.cand: tuple[int, int] | None = None
        self.cand_n = 0
        self.change_t: float | None = None
        self.last_text_t = -1e9
        self.bar: float | None = None
        self.bar_cand: float | None = None
        self.bar_n = 0
        self.bar_change_t: float | None = None
        self.dead = False

    def update_text(self, t: float, raw: str) -> list[Event]:
        m = _HP_RE.search(raw or "")
        if not m:
            return []
        cur, mx = int(m.group(1)), int(m.group(2))
        if mx <= 0 or cur > mx or mx > 20000:
            return []
        self.last_text_t = t
        val = (cur, mx)
        if self.cur is not None and val == (self.cur, self.max):
            self.cand, self.cand_n, self.change_t = None, 0, None
            return []
        if self.change_t is None:
            self.change_t = t
        if val == self.cand:
            self.cand_n += 1
        else:
            self.cand, self.cand_n = val, 1
        if self.cand_n < self.cfg["confirm_frames"]:
            return []
        ev_t = self.change_t + self.tcorr
        old_cur, old_max = self.cur, self.max
        self.cur, self.max = cur, mx
        self.cand, self.cand_n, self.change_t = None, 0, None
        out: list[Event] = []
        if old_cur is None or old_max != mx:
            self.dead = cur == 0
            return out
        if cur < old_cur:
            drop = old_cur - cur
            if drop >= self.cfg["min_drop"] or cur == 0:
                out.append(Event(ev_t, E.DAMAGE_TAKEN, "vision:міцність", value=drop,
                                 detail=f"{old_cur} → {cur} / {mx}"))
            if cur == 0 and not self.dead:
                out.append(Event(ev_t, E.DEATH, "vision:міцність", value=0, detail=f"міцність 0 / {mx}"))
                self.dead = True
        elif cur > old_cur and cur == mx and old_cur < mx * 0.5:
            self.dead = False  # новий бій
        return out

    def update_bar(self, t: float, frac: float | None) -> list[Event]:
        if frac is None:
            return []
        if t - self.last_text_t < 3.0:  # текст працює — смужку не використовуємо
            self.bar = frac
            return []
        if self.bar is None:
            self.bar = frac
            return []
        if abs(frac - self.bar) < 0.01:
            self.bar_cand, self.bar_n, self.bar_change_t = None, 0, None
            return []
        if self.bar_change_t is None:
            self.bar_change_t = t
        if self.bar_cand is not None and abs(frac - self.bar_cand) < 0.01:
            self.bar_n += 1
        else:
            self.bar_cand, self.bar_n = frac, 1
        if self.bar_n < self.cfg["confirm_frames"]:
            return []
        old = self.bar
        self.bar = frac
        ev_t = self.bar_change_t + self.tcorr
        self.bar_cand, self.bar_n, self.bar_change_t = None, 0, None
        if old - frac >= self.cfg["bar_min_drop"] and frac > 0.005:
            return [Event(ev_t, E.DAMAGE_TAKEN, "vision:смужка", value=None, confidence=0.6,
                          detail=f"смужка {old:.0%} → {frac:.0%}")]
        return []


class LogTracker:
    """Лог подій: нові зелені рядки — моя шкода/знищення, червоні — отримана шкода."""

    def __init__(self, cfg: dict, tcorr: float):
        self.cfg = cfg
        self.tcorr = tcorr
        self.memory: list[tuple[str, str, float]] = []  # (колір, текст, коли бачили востаннє)

    def _known(self, color: str, norm: str, t: float) -> bool:
        keep = []
        found = False
        for c, txt, seen in self.memory:
            if t - seen > self.cfg["memory_s"]:
                continue
            if not found and c == color and (txt == norm or partial_ratio(norm, txt) >= self.cfg["match_ratio"]
                                             or partial_ratio(txt, norm) >= self.cfg["match_ratio"]):
                found = True
                keep.append((c, txt, t))
            else:
                keep.append((c, txt, seen))
        self.memory = keep
        return found

    def update(self, t: float, lines: list[dict]) -> list[Event]:
        out: list[Event] = []
        for line in lines or []:
            text = line.get("text", "")
            color = line.get("color", "white")
            if color not in ("green", "red"):
                continue
            norm = re.sub(r"\s+", " ", text.lower()).strip()
            if len(norm.replace(" ", "")) < self.cfg["min_chars"]:
                continue
            if self._known(color, norm, t):
                continue
            self.memory.append((color, norm, t))
            value = first_number(text)
            is_kill = contains_keyword(text, self.cfg["kill_keywords"], self.cfg["keyword_ratio"]) is not None
            ev_t = t + self.tcorr
            if color == "green" and is_kill:
                out.append(Event(ev_t, E.KILL, "vision:лог", value=value, detail=text))
            elif color == "green" and value:
                out.append(Event(ev_t, E.LOG_DAMAGE, "vision:лог", value=value, detail=text))
            elif color == "red" and value:
                out.append(Event(ev_t, E.DAMAGE_TAKEN, "vision:лог", value=value, detail=text))
        return out


class EndScreenTracker:
    """Екран смерті / результатів. Подія — лише при ПОЯВІ напису: поки ви спостерігаєте
    за боєм після смерті, напис висить постійно, і повторних подій бути не повинно."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.last_seen: dict[str, float] = {}
        self.cache: tuple[str, str | None, str | None] = ("", None, None)

    def update(self, t: float, text: str) -> list[Event]:
        if not text:
            return []
        if text != self.cache[0]:
            kw = contains_keyword(text, self.cfg["death_keywords"], self.cfg["keyword_ratio"])
            kw2 = contains_keyword(text, self.cfg["battle_end_keywords"], self.cfg["keyword_ratio"])
            self.cache = (text, kw, kw2)
        _, kw, kw2 = self.cache
        out = []
        for typ, word in ((E.DEATH, kw), (E.BATTLE_END, kw2)):
            if not word:
                continue
            prev = self.last_seen.get(typ, -1e9)
            self.last_seen[typ] = t
            if t - prev > self.cfg["cooldown_s"]:
                out.append(Event(t, typ, "vision:екран", detail=f"«{word}»",
                                 confidence=0.9 if typ == E.DEATH else 1.0))
        return out

    def reset(self):
        self.last_seen.clear()


def analyze_readings(readings: list[dict], profile: dict) -> list[Event]:
    """Показання -> сирі події (до злиття дублів)."""
    v = profile["vision"]
    tc = float(v.get("time_correction_s", 0.0))
    dmg = DamageTracker(v["damage_counter"], tc)
    hp = HpTracker(v["hp"], tc)
    lg = LogTracker(v["event_log"], tc)
    end = EndScreenTracker(v["end_screen"])
    out: list[Event] = []
    for r in readings:
        t = r["t"]
        if "dmg_raw" in r:
            evs = dmg.update(t, r["dmg_raw"])
            if any(e.type == E.BATTLE_START for e in evs):
                end.reset()
            out += evs
        if "hp_raw" in r:
            out += hp.update_text(t, r["hp_raw"])
        if "hp_bar" in r:
            out += hp.update_bar(t, r["hp_bar"])
        if "log" in r:
            out += lg.update(t, r["log"])
        if "end" in r:
            out += end.update(t, r["end"])
    out.sort(key=lambda e: e.t)
    return out


def merge_events(evs: list[Event], profile: dict) -> list[Event]:
    """Зливає дублікати з різних зон в одну подію."""
    b = profile["battles"]
    evs = sorted(evs, key=lambda e: e.t)
    hits = [e for e in evs if e.type == E.HIT]
    out: list[Event] = []

    # 1) Зелений рядок логу з числом підтверджує влучання з лічильника.
    for e in evs:
        if e.type != E.LOG_DAMAGE:
            continue
        near = [h for h in hits if abs(h.t - e.t) <= b["log_match_s"]]
        if near:
            h = min(near, key=lambda h: (abs((h.value or 0) - (e.value or 0)) > 5, abs(h.t - e.t)))
            h.confidence = min(1.0, h.confidence + 0.3)
            if "+лог" not in h.source:
                h.source += "+лог"
        else:
            out.append(Event(e.t, E.HIT, "vision:лог", value=e.value, confidence=0.6, detail=e.detail))

    # 2) Отримана шкода з логу й з міцності — одна подія.
    taken = sorted([e for e in evs if e.type == E.DAMAGE_TAKEN], key=lambda e: e.t)
    merged_taken: list[Event] = []
    for e in taken:
        if merged_taken and e.t - merged_taken[-1].t <= 1.5:
            m = merged_taken[-1]
            if m.value is None and e.value is not None:
                m.value = e.value
            if e.source not in m.source:
                m.source += "+" + e.source.split(":")[-1]
            m.confidence = min(1.0, max(m.confidence, e.confidence) + 0.2)
            continue
        merged_taken.append(e)

    # 3) Смерть: міцність 0 і екран «Оцініть бій» — одна подія (найраніша).
    deaths = sorted([e for e in evs if e.type == E.DEATH], key=lambda e: e.t)
    merged_deaths: list[Event] = []
    for e in deaths:
        if merged_deaths and e.t - merged_deaths[-1].t <= b["merge_death_s"]:
            m = merged_deaths[-1]
            if e.source not in m.source:
                m.source += "+" + e.source.split(":")[-1]
                m.detail += "; " + e.detail
            m.confidence = 1.0
            continue
        merged_deaths.append(e)

    # 4) Знищення ворога — без дублів у межах 3 с.
    kills: list[Event] = []
    for e in sorted([e for e in evs if e.type == E.KILL], key=lambda e: e.t):
        if kills and e.t - kills[-1].t <= 3.0:
            continue
        kills.append(e)

    other = [e for e in evs if e.type in (E.HIT, E.BATTLE_START, E.BATTLE_END)]
    out += other + merged_taken + merged_deaths + kills
    out.sort(key=lambda e: (e.t, e.type))
    return out


def split_battles(evs: list[Event], profile: dict, start: float, end: float) -> list[dict]:
    """Поділ на бої: обнулення лічильника, смерть/кінець бою + пауза, довга пауза."""
    gap = float(profile["battles"]["split_gap_s"])
    battles: list[dict] = []
    cur: dict | None = None
    finished = False
    last_t = start
    for e in sorted(evs, key=lambda e: e.t):
        new = False
        if e.type == E.BATTLE_START:
            new = cur is not None
        elif cur is not None and e.t - last_t > gap and (finished or e.t - last_t > 4 * gap):
            new = True
        if cur is None or new:
            if cur is not None:
                battles.append(cur)
            cur = {"start": max(start, e.t - 5.0) if e.type != E.BATTLE_START else e.t, "events": []}
            finished = False
        if e.type != E.BATTLE_START:
            cur["events"].append(e)
        if e.type in (E.DEATH, E.BATTLE_END):
            finished = True
        last_t = e.t
    if cur is not None:
        battles.append(cur)

    result = []
    for i, b in enumerate(battles, 1):
        es = b["events"]
        if not es:
            continue
        hits = [e for e in es if e.type == E.HIT]
        deaths = [e for e in es if e.type == E.DEATH]
        ends = [e for e in es if e.type == E.BATTLE_END]
        counters = [e.extra.get("counter") for e in hits if e.extra.get("counter")]
        result.append({
            "index": len(result) + 1,
            "start": b["start"],
            "end": min(end, max(e.t for e in es) + 3.0),
            "hits": len(hits),
            "damage_sum": int(sum(e.value or 0 for e in hits)),
            "damage_counter": max(counters) if counters else None,
            "kills": sum(1 for e in es if e.type == E.KILL),
            "damage_taken": sum(1 for e in es if e.type == E.DAMAGE_TAKEN),
            "died": bool(deaths),
            "death_t": deaths[0].t if deaths else None,
            "ended_by": ("смерть" if deaths else "кінець бою" if ends else "пауза/кінець запису"),
            "events": es,
        })
    return result


def detect(settings: dict, profile: dict, info: VideoInfo, start: float = 0.0, end: float | None = None,
           debug_dir: Path | None = None, progress=None, readings: list[dict] | None = None):
    """Повний зоровий детектор. Повертає (події, бої, показання)."""
    if readings is None:
        scanner = VisionScanner(settings, profile, info, debug_dir=debug_dir)
        readings = scanner.scan(start, end, progress=progress)
    raw = analyze_readings(readings, profile)
    evs = merge_events(raw, profile)
    end_t = info.duration if end is None else min(end, info.duration)
    battles = split_battles(evs, profile, start, end_t)
    return evs, battles, readings


def readings_cache_files(settings: dict, profile: dict, info: VideoInfo) -> list[Path]:
    return sorted(_cache_dir(settings, info, profile).glob("chunk_*.jsonl"))


def load_cached_readings(settings: dict, profile: dict, info: VideoInfo,
                         start: float, end: float | None) -> list[dict] | None:
    files = readings_cache_files(settings, profile, info)
    if not files:
        return None
    end = info.duration if end is None else end
    by_t: dict[float, dict] = {}
    for f in files:
        for l in f.read_text(encoding="utf-8").splitlines():
            if l:
                r = json.loads(l)
                if start <= r["t"] < end:
                    by_t[round(r["t"], 2)] = r
    out = [by_t[k] for k in sorted(by_t)]
    return out or None


__all__ = ["detect", "VisionScanner", "analyze_readings", "merge_events", "split_battles",
           "load_cached_readings", "classify_color", "bar_fill", "normalize_words"]
