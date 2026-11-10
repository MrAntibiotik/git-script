"""Спільний формат подій (моментів) для всіх детекторів."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

# Типи подій та їхні назви українською.
HIT = "hit"                    # я влучив (лічильник шкоди зріс)
DAMAGE_TAKEN = "damage_taken"  # мене влучили
KILL = "kill"                  # я знищив ворога
DEATH = "death"                # мене знищено / екран смерті
BATTLE_START = "battle_start"  # початок бою (лічильник обнулився)
BATTLE_END = "battle_end"      # екран результатів / кінець бою
LOG_DAMAGE = "log_damage"      # зелений рядок логу з числом (службова, зливається з HIT)
GAME_SOUND = "game_sound"      # звук гри зі зразків (етап 2)
VOICE = "voice"                # моя емоція (етап 2)

LABELS = {
    HIT: "Влучання",
    DAMAGE_TAKEN: "Отримав шкоду",
    KILL: "Знищив ворога",
    DEATH: "Мене знищено",
    BATTLE_START: "Початок бою",
    BATTLE_END: "Кінець бою",
    LOG_DAMAGE: "Лог: моя шкода",
    GAME_SOUND: "Звук гри",
    VOICE: "Голос/емоція",
}


@dataclass
class Event:
    t: float
    type: str
    source: str
    value: float | None = None
    confidence: float = 1.0
    detail: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def label(self) -> str:
        return LABELS.get(self.type, self.type)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["label"] = self.label
        return d

    @staticmethod
    def from_dict(d: dict) -> "Event":
        return Event(t=float(d["t"]), type=d["type"], source=d.get("source", ""),
                     value=d.get("value"), confidence=float(d.get("confidence", 1.0)),
                     detail=d.get("detail", ""), extra=d.get("extra") or {})
