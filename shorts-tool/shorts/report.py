"""Звіти: CSV (відкривається в Excel) і JSON.

CSV з роздільником «;» і кодуванням UTF-8 з BOM — так українська Excel
відкриває його подвійним кліком без «кракозябр».
"""

from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path

from .events import Event
from .util import fmt_time

CSV_FIELDS = ["бій", "час", "секунда", "тип", "подія", "шкода/значення", "score",
              "джерело", "впевненість", "деталі", "кліп"]


def _json_default(o):
    """numpy-числа та інші типи -> звичайні значення для JSON."""
    try:
        import numpy as np
        if isinstance(o, np.generic):
            return o.item()
        if isinstance(o, np.ndarray):
            return o.tolist()
    except ImportError:
        pass
    if isinstance(o, Path):
        return str(o)
    return str(o)


def event_row(e: Event, battle: int | None, clip: str = "") -> dict:
    return {
        "бій": battle or "",
        "час": fmt_time(e.t),
        "секунда": f"{e.t:.2f}",
        "тип": e.type,
        "подія": e.label,
        "шкода/значення": "" if e.value is None else (int(e.value) if float(e.value).is_integer() else e.value),
        "score": e.extra.get("score", ""),
        "джерело": e.source,
        "впевненість": f"{e.confidence:.2f}",
        "деталі": e.detail,
        "кліп": clip or e.extra.get("clip", ""),
    }


def write_reports(out_dir: Path, base_name: str, video: Path, profile_id: str,
                  events: list[Event], battles: list[dict], extra: dict | None = None) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / f"{base_name}.csv"
    json_path = out_dir / f"{base_name}.json"

    battle_of = {}
    for b in battles:
        for e in b["events"]:
            battle_of[id(e)] = b["index"]

    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS, delimiter=";")
        w.writeheader()
        for e in sorted(events, key=lambda e: e.t):
            w.writerow(event_row(e, battle_of.get(id(e))))

    data = {
        "створено": datetime.now().isoformat(timespec="seconds"),
        "запис": str(video),
        "профіль": profile_id,
        "бої": [
            {k: v for k, v in b.items() if k != "events"}
            | {"початок_час": fmt_time(b["start"]), "кінець_час": fmt_time(b["end"]),
               "події": [e.to_dict() | {"час": fmt_time(e.t)} for e in b["events"]]}
            for b in battles
        ],
        "усі_події": [e.to_dict() | {"час": fmt_time(e.t), "бій": battle_of.get(id(e))} for e in events],
    }
    if extra:
        data.update(extra)
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=_json_default)
    return csv_path, json_path
