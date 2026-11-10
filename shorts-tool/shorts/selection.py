"""Відбір моментів: ОДИН бій = ОДИН шортс до 60 с.

Для кожного бою:
  1. Беремо моменти зі score (з fusion.py) не слабші за min_score.
  2. Фінальний момент бою (моя смерть або останнє знищення ворога) включаємо завжди.
  3. Навколо кожного моменту — запас до/після (сильніший момент — більший запас).
  4. Додаємо моменти від найсильнішого до слабшого, доки сумарна тривалість ≤ 60 с.
     Близькі шматки (пауза < merge_gap_s) зливаються в один.
  5. Межі шматків підганяються під паузи в мові (не різати фразу посередині).
  6. Шматки впорядковуються хронологічно.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np

from . import events as E
from .audio import AudioChunk
from .dsp import frame_rms_db
from .events import Event
from .util import fmt_time

log = logging.getLogger("selection")


# ====================================================================== паузи в мові

class SpeechMap:
    """Де ви говорите (за доріжкою голосу): 50 значень на секунду."""

    STEP = 0.02

    def __init__(self, chunks: list[AudioChunk], margin_db: float):
        self.parts: list[tuple[float, np.ndarray]] = []
        for ch in chunks:
            y, sr = ch.load("voice")
            db, step = frame_rms_db(y, sr, hop_s=self.STEP, win_s=0.04)
            noise = float(np.percentile(db, 15))
            speech = db > max(noise + margin_db, -55.0)
            # дрібні дірки (між складами) не вважаються паузою
            speech = _close_gaps(speech, int(0.12 / self.STEP))
            self.parts.append((ch.c0, speech))

    def _get(self, t: float) -> bool | None:
        for c0, arr in self.parts:
            i = int((t - c0) / self.STEP)
            if 0 <= i < len(arr):
                return bool(arr[i])
        return None

    def is_speech(self, t: float) -> bool:
        return bool(self._get(t))

    def find_pause(self, t: float, direction: int, max_s: float, min_pause_s: float) -> float | None:
        """Найближча точка в паузі (тиші ≥ min_pause_s) у напрямку direction (−1 назад, +1 вперед)."""
        if not self.is_speech(t):
            return t
        need = max(1, int(min_pause_s / self.STEP))
        run = 0
        steps = int(max_s / self.STEP)
        for k in range(1, steps + 1):
            tt = t + direction * k * self.STEP
            v = self._get(tt)
            if v is None:
                return None
            if not v:
                run += 1
                if run >= need:
                    # точка всередині паузи, ближча до мови (щоб не брати зайву тишу)
                    return tt - direction * (need - 1) * self.STEP * 0.5
            else:
                run = 0
        return None


def _close_gaps(mask: np.ndarray, max_gap: int) -> np.ndarray:
    m = mask.copy()
    idx = np.flatnonzero(mask)
    if idx.size < 2:
        return m
    gaps = np.diff(idx)
    for i in np.flatnonzero((gaps > 1) & (gaps <= max_gap + 1)):
        m[idx[i]:idx[i + 1]] = True
    return m


# ====================================================================== моделі

@dataclass
class Segment:
    start: float
    end: float
    events: list[Event] = field(default_factory=list)

    @property
    def duration(self) -> float:
        return self.end - self.start

    @property
    def score(self) -> float:
        return sum(float(e.extra.get("score", 0)) for e in self.events)


@dataclass
class Short:
    index: int
    battle: int
    segments: list[Segment]
    final_event: Event | None
    title: str = ""
    name: str = ""

    @property
    def duration(self) -> float:
        return sum(s.duration for s in self.segments)

    @property
    def score(self) -> float:
        return sum(s.score for s in self.segments)

    @property
    def events(self) -> list[Event]:
        return [e for s in self.segments for e in s.events]

    def src_to_short(self, t: float) -> float | None:
        """Час у записі -> час у шортсі (None, якщо поза шматками)."""
        acc = 0.0
        for s in self.segments:
            if s.start <= t <= s.end:
                return acc + (t - s.start)
            acc += s.duration
        return None


# ====================================================================== відбір

def _pad(e: Event, rng: list[float], max_score: float) -> float:
    lo, hi = float(rng[0]), float(rng[1])
    k = min(1.0, float(e.extra.get("score", 0)) / max(max_score, 1e-6))
    return lo + (hi - lo) * k


def _window(e: Event, cfg: dict, max_score: float) -> tuple[float, float]:
    pre = _pad(e, cfg["pre_s"], max_score)
    post = cfg.get("death_post_s", 1.5) if e.type == E.DEATH else _pad(e, cfg["post_s"], max_score)
    return e.t - pre, e.t + post


def _merge(segs: list[Segment], gap: float) -> list[Segment]:
    segs = sorted(segs, key=lambda s: s.start)
    out: list[Segment] = []
    for s in segs:
        if out and s.start - out[-1].end <= gap:
            o = out[-1]
            o.end = max(o.end, s.end)
            o.events += [e for e in s.events if e not in o.events]
        else:
            out.append(Segment(s.start, s.end, list(s.events)))
    return out


def _total(segs: list[Segment]) -> float:
    return sum(s.duration for s in segs)


def select_battle(battle: dict, cfg: dict, rec_start: float, rec_end: float,
                  speech: SpeechMap | None) -> tuple[list[Segment], Event | None]:
    moments = [e for e in battle["events"] if e.extra.get("moment")]
    if not moments:
        return [], None
    final = None
    if cfg["always_include_final"]:
        finals = [e for e in moments if e.type in (E.DEATH, E.KILL)]
        if finals:
            final = max(finals, key=lambda e: e.t)
    strong = [e for e in moments if float(e.extra.get("score", 0)) >= cfg["min_score"]]
    max_score = max((float(e.extra.get("score", 0)) for e in moments), default=1.0)
    order = sorted(strong, key=lambda e: -float(e.extra.get("score", 0)))
    if final is not None:
        order = [final] + [e for e in order if e is not final]

    limit = float(cfg["max_duration_s"])
    gap = float(cfg["merge_gap_s"])
    chosen: list[Segment] = []
    for e in order:
        a, b = _window(e, cfg, max_score)
        a, b = max(rec_start, a), min(rec_end, b)
        trial = _merge(chosen + [Segment(a, b, [e])], gap)
        if _total(trial) <= limit:
            chosen = trial
        elif not chosen:  # навіть один момент не влазить — обрізаємо навколо нього
            half = limit / 2
            chosen = [Segment(max(rec_start, e.t - half), min(rec_end, e.t + half), [e])]

    if speech is not None and cfg["speech_snap"]:
        chosen = _snap(chosen, cfg, speech, rec_start, rec_end, limit, gap)

    # події, що випадково потрапили в шматок, теж належать йому (для маркерів і звіту)
    for s in chosen:
        for e in battle["events"]:
            if s.start <= e.t <= s.end and e not in s.events and e.type not in (E.BATTLE_START, E.BATTLE_END):
                s.events.append(e)
        s.events.sort(key=lambda e: e.t)
    return sorted(chosen, key=lambda s: s.start), final


def _snap(segs: list[Segment], cfg: dict, speech: SpeechMap, rec_start: float, rec_end: float,
          limit: float, gap: float) -> list[Segment]:
    mx, mp = float(cfg["snap_max_s"]), float(cfg["min_pause_s"])
    out = []
    for s in segs:
        first_ev = min(e.t for e in s.events)
        last_ev = max(e.t for e in s.events)
        a, b = s.start, s.end
        na = speech.find_pause(a, -1, mx, mp)                       # раніше
        if na is None:
            na = speech.find_pause(a, +1, min(mx / 2, max(0.0, first_ev - 0.5 - a)), mp)  # або трохи пізніше
        nb = speech.find_pause(b, +1, mx, mp)                       # пізніше
        if nb is None:
            nb = speech.find_pause(b, -1, min(mx / 2, max(0.0, b - last_ev - 0.5)), mp)
        out.append(Segment(max(rec_start, na if na is not None else a),
                           min(rec_end, nb if nb is not None else b), list(s.events)))
    out = _merge(out, gap)
    # якщо після підгонки вийшло довше за ліміт — повертаємо найслабші межі назад
    if _total(out) > limit:
        out = _merge([Segment(s.start, s.end, list(s.events)) for s in segs], gap)
    return out


def select_shorts(battles: list[dict], profile: dict, rec_start: float, rec_end: float,
                  speech: SpeechMap | None = None) -> list[Short]:
    cfg = profile["selection"]
    shorts: list[Short] = []
    skipped = []
    for b in battles:
        segs, final = select_battle(b, cfg, rec_start, rec_end, speech)
        if not segs:
            skipped.append((b["index"], "немає моментів"))
            continue
        sc = sum(s.score for s in segs)
        dur = _total(segs)
        if sc < cfg["min_battle_score"]:
            skipped.append((b["index"], f"слабкий бій (score {sc:.1f})"))
            continue
        if dur < cfg["min_short_s"]:
            skipped.append((b["index"], f"закоротко ({dur:.1f} с)"))
            continue
        sh = Short(index=0, battle=b["index"], segments=segs, final_event=final)
        sh.title = make_title(sh, b)
        shorts.append(sh)
    if cfg.get("max_shorts"):
        shorts = sorted(shorts, key=lambda s: -s.score)[: int(cfg["max_shorts"])]
    shorts.sort(key=lambda s: s.segments[0].start)
    for i, sh in enumerate(shorts, 1):
        sh.index = i
        sh.name = f"Шортс {i:02d} — {sh.title}"
    for idx, why in skipped:
        log.info("  Бій %d пропущено: %s", idx, why)
    return shorts


def make_title(sh: Short, battle: dict) -> str:
    evs = sh.events
    dmg = battle.get("damage_counter") or sum(int(e.value or 0) for e in evs if e.type == E.HIT)
    kills = sum(1 for e in evs if e.type == E.KILL)
    parts = [f"бій {battle['index']}"]
    if dmg:
        parts.append(f"{dmg} шкоди")
    if kills:
        parts.append(f"знищень: {kills}")
    if any(e.type == E.DEATH for e in evs):
        parts.append("до останнього")
    emos = {e.extra.get("emotion") for e in evs if e.extra.get("emotion")}
    if emos:
        parts.append("/".join(sorted(emos)))
    return ", ".join(parts)


def describe(sh: Short) -> str:
    segs = "  ".join(f"{fmt_time(s.start, 0)}–{fmt_time(s.end, 0)}" for s in sh.segments)
    return f"{sh.name}: {sh.duration:.1f} с, score {sh.score:.1f} | {segs}"
