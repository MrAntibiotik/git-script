"""Об'єднання сигналів (екран + звук гри + голос) і оцінка сили моменту (score).

Правила (числа — у профілі гри, розділ "scoring"):
  - базовий score за тип: влучання, знищення ворога, моя смерть, отримав шкоду…;
  - влучання: + шкода / hit_damage_scale (не більше hit_damage_cap) — сильніший постріл вище;
  - подія з екрану, підтверджена звуком гри в межах ±sound_confirm_window_s → × sound_confirm_mult;
  - моя емоційна реакція через 0–3 с після події → + emotion_bonus × інтенсивність;
  - для ігор без зорових сигналів моментами стають самі звуки гри й голосові реакції
    (audio_only_moments / voice_only_moments).
"""

from __future__ import annotations

from . import events as E
from .events import Event

VISION_MOMENTS = {E.HIT, E.KILL, E.DEATH, E.DAMAGE_TAKEN}


def _base(e: Event, sc: dict) -> float:
    b = float(sc["base"].get(e.type, 0.5))
    if e.type == E.HIT and e.value:
        if float(e.value) < float(sc.get("small_hit_damage", 0)):
            return float(sc.get("small_hit_base", 0.3))   # дрібна шкода (пожежа, таран) — не момент
        b += min(sc["hit_damage_cap"], float(e.value) / sc["hit_damage_scale"])
    elif e.type == E.DAMAGE_TAKEN and e.value:
        b += min(1.5, float(e.value) / sc["taken_damage_scale"])
    return b


def fuse(vision: list[Event], sounds: list[Event], voices: list[Event], profile: dict) -> list[Event]:
    """Повертає всі події з extra["score"] і extra["moment"] (чи це кандидат у шортс)."""
    sc = profile["scoring"]
    w = float(sc["sound_confirm_window_s"])
    e0, e1 = sc["emotion_window_s"]
    used_sound: set[int] = set()
    used_voice: set[int] = set()
    out: list[Event] = []

    for e in sorted(vision, key=lambda e: e.t):
        if e.type not in VISION_MOMENTS:
            e.extra.update(score=0.0, moment=False)
            out.append(e)
            continue
        score = _base(e, sc)
        notes = []
        # підтвердження звуком гри
        conf = [(i, s) for i, s in enumerate(sounds) if abs(s.t - e.t) <= w]
        if conf:
            i, s = max(conf, key=lambda p: p[1].confidence * p[1].extra.get("weight", 0.5))
            used_sound.update(i for i, _ in conf)
            score *= float(sc["sound_confirm_mult"])
            notes.append(f"звук: {s.extra.get('category')} ({s.confidence:.2f})")
            e.extra["sound_confirmed"] = True
        # емоція після події
        emo = [(i, v) for i, v in enumerate(voices) if e0 <= v.t - e.t <= e1]
        if emo:
            i, v = max(emo, key=lambda p: p[1].extra.get("intensity", 1.0))
            used_voice.update(i for i, _ in emo)
            bonus = float(sc["emotion_bonus"]) * float(v.extra.get("intensity", 1.0))
            score += bonus
            notes.append(f"реакція: {v.extra.get('kind')} через {v.t - e.t:.1f} с")
            e.extra["emotion"] = v.extra.get("kind")
        e.extra.update(score=round(score, 2), moment=True, why="; ".join(notes))
        out.append(e)

    for i, s in enumerate(sounds):
        is_moment = i not in used_sound and bool(sc["audio_only_moments"])
        score = float(sc["base"].get(E.GAME_SOUND, 0.5)) * float(s.extra.get("weight", 0.5)) * \
            (0.5 + s.confidence) if is_moment else 0.0
        s.extra.update(score=round(score, 2), moment=is_moment and score >= sc["min_score_audio_only"],
                       used_for_confirm=i in used_sound)
        out.append(s)

    for i, v in enumerate(voices):
        is_moment = i not in used_voice and bool(sc["voice_only_moments"])
        score = float(sc["base"].get(E.VOICE, 0.8)) * (1.0 + float(v.extra.get("intensity", 1.0))) \
            if is_moment else 0.0
        v.extra.update(score=round(score, 2), moment=is_moment and score >= sc["min_score_audio_only"],
                       used_for_bonus=i in used_voice)
        out.append(v)

    # Звук+голос разом (для ігор без екрану): реакція після звуку гри підсилює звук.
    if sc["audio_only_moments"]:
        moments = [s for s in sounds if s.extra.get("moment")]
        for s in moments:
            emo = [v for v in voices if e0 <= v.t - s.t <= e1]
            if emo:
                v = max(emo, key=lambda v: v.extra.get("intensity", 1.0))
                s.extra["score"] = round(s.extra["score"] + float(sc["emotion_bonus"]) *
                                         float(v.extra.get("intensity", 1.0)), 2)
                s.extra["why"] = f"реакція: {v.extra.get('kind')} через {v.t - s.t:.1f} с"
                if v.extra.get("moment"):
                    v.extra["moment"] = False   # вже врахована в звуку
    out.sort(key=lambda e: e.t)
    return out


def split_by_gaps(events: list[Event], gap_s: float, start: float, end: float) -> list[dict]:
    """Поділ на «матчі» для ігор без зорових ознак: скупчення моментів, розділені паузою > gap_s."""
    ms = sorted([e for e in events if e.extra.get("moment")], key=lambda e: e.t)
    groups: list[list[Event]] = []
    for e in ms:
        if groups and e.t - groups[-1][-1].t <= gap_s:
            groups[-1].append(e)
        else:
            groups.append([e])
    out = []
    for i, g in enumerate(groups, 1):
        out.append({"index": i, "start": max(start, g[0].t - 5), "end": min(end, g[-1].t + 5),
                    "hits": 0, "damage_sum": 0, "damage_counter": None, "kills": 0, "damage_taken": 0,
                    "died": False, "death_t": None, "ended_by": "пауза", "events": g})
    return out


def assign_to_battles(battles: list[dict], extra_events: list[Event]) -> None:
    """Додає звукові/голосові події до боїв, у межі яких вони потрапляють."""
    for b in battles:
        ids = {id(e) for e in b["events"]}
        for e in extra_events:
            if b["start"] <= e.t <= b["end"] + 3 and id(e) not in ids:
                b["events"].append(e)
        b["events"].sort(key=lambda e: e.t)
