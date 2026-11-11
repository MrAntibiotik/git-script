"""Розклад і черга: сканування ready/, слоти, сповіщення за годину, пропуски, звіти."""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import media
from config import fmt_slot, local_dt, now_ts
from meta import json_status, load_meta, move_pair, read_json, write_json

log = logging.getLogger("scheduler")


class Scheduler:
    def __init__(self, app):
        self.app = app
        self.cfg = app.cfg
        self.db = app.db
        self._last_scan = 0
        self._stats_thread: threading.Thread | None = None

    # ------------------------------------------------------------------
    def tick(self, now: int | None = None) -> None:
        now = now or now_ts()
        if now - self._last_scan >= int(self.cfg["scan_interval_seconds"]):
            self._last_scan = now
            self.scan(now)
        self.ensure_slots(now)
        self.resume_publishing()
        self.process_due_slots(now)
        self.process_notifications(now)
        self.maybe_daily_report(now)
        self.maybe_stats(now)

    # ---------- черга ----------
    def scan(self, now: int | None = None) -> list[int]:
        """Додає нові ready/<Гра>/*.mp4 (з json у статусі ready_to_post) у кінець черги (FIFO)."""
        now = now or now_ts()
        ready = Path(self.cfg["paths"]["ready"])
        if not ready.exists():
            ready.mkdir(parents=True, exist_ok=True)
            return []
        known = self.db.known_paths()
        found = []
        for mp4 in list(ready.glob("*.mp4")) + list(ready.glob("*/*.mp4")):
            path = str(mp4.resolve())
            if path in known:
                continue
            js = mp4.with_suffix(".json")
            if not js.exists():
                continue
            try:
                if time.time() - max(mp4.stat().st_mtime, js.stat().st_mtime) < 30:
                    continue  # файл ще записується
                data = read_json(js)
            except (OSError, ValueError):
                continue
            if json_status(data) != self.cfg["ready_status"]:
                continue
            game = mp4.parent.name if mp4.parent != ready else ""
            found.append((js.stat().st_mtime, path, str(js.resolve()), game))
        added = []
        for _, path, js, game in sorted(found):
            item_id = self.db.add_item(path, js, game)
            if item_id:
                added.append(item_id)
                self.db.event(item_id, "queued", {"file": Path(path).name})
                log.info("У чергу додано #%s: %s", item_id, path)
        return added

    # ---------- слоти ----------
    def _day_slots(self, day) -> list[int]:
        out = []
        for hm in self.cfg["slots"]:
            h, m = map(int, str(hm).split(":"))
            out.append(int(datetime(day.year, day.month, day.day, h, m, tzinfo=self.cfg["tz"]).timestamp()))
        return out

    def ensure_slots(self, now: int) -> None:
        today = local_dt(self.cfg, now).date()
        wanted = set()
        for d in (today, today + timedelta(days=1)):
            wanted.update(self._day_slots(d))
        for ts in wanted:
            self.db.x("INSERT OR IGNORE INTO slots(slot_at, source, state) VALUES (?, 'config', 'pending')", (ts,))
        # Слоти, прибрані з config.json, ще не використані — видаляємо
        for s in self.db.q("SELECT slot_at FROM slots WHERE source='config' AND state='pending' AND slot_at>?", (now,)):
            if s["slot_at"] not in wanted:
                self.db.x("DELETE FROM slots WHERE slot_at=?", (s["slot_at"],))

    def add_manual_slot(self, minutes: int) -> int:
        minutes = max(int(minutes), int(self.cfg["min_notify_lead_minutes"]) + 2)
        ts = (now_ts() + minutes * 60 + 59) // 60 * 60
        while self.db.slot(ts):
            ts += 60
        self.db.x("INSERT INTO slots(slot_at, source, state) VALUES (?, 'manual', 'pending')", (ts,))
        log.info("Додано ручний слот %s", fmt_slot(self.cfg, ts))
        return ts

    def upcoming(self, now: int, limit: int = 6) -> list[dict]:
        return self.db.q("SELECT * FROM slots WHERE slot_at>? AND state IN ('pending','notified') "
                         "ORDER BY slot_at LIMIT ?", (now, limit))

    # ---------- відновлення ----------
    def resume_publishing(self) -> None:
        for item in self.db.items("status='publishing'"):
            if not self.app.publisher.is_active(item["id"]):
                log.info("Відновлюю перервану публікацію #%s", item["id"])
                self.app.publisher.start(item["id"])

    # ---------- настання слоту ----------
    def process_due_slots(self, now: int) -> None:
        for s in self.db.q("SELECT * FROM slots WHERE state IN ('pending','notified') AND slot_at<=? "
                           "ORDER BY slot_at", (now,)):
            if s["state"] == "pending":
                self.db.set_slot(s["slot_at"], state="empty")
                self.db.event(None, "empty_slot", {"slot": s["slot_at"]})
                continue
            item = self.db.get_item(s["item_id"]) if s["item_id"] else None
            if not item or item["slot_at"] != s["slot_at"]:
                self.db.set_slot(s["slot_at"], state="done")
                continue
            if item["status"] == "approved":
                late = now - s["slot_at"]
                if late <= int(self.cfg["catchup_minutes"]) * 60:
                    self.db.set_slot(s["slot_at"], state="done")
                    log.info("Слот %s: публікую #%s", fmt_slot(self.cfg, s["slot_at"]), item["id"])
                    self.app.publisher.start(item["id"])
                else:  # ПК був вимкнений під час слоту — повертаємо на початок черги без штрафу
                    self.db.set_slot(s["slot_at"], state="missed")
                    self.db.update_item(item["id"], status="queued", qpos=self.db.first_qpos(), slot_at=None,
                                        note="↩️ Слот минув, поки ПК був вимкнений — шортс повернуто на початок черги")
                    self.app.bot.refresh(item["id"])
                    self.db.update_item(item["id"], note=None)
            elif item["status"] == "notified":
                self.db.set_slot(s["slot_at"], state="skipped")
                self.skip(item)
            else:
                self.db.set_slot(s["slot_at"], state="done")

    def skip(self, item: dict) -> None:
        skips = item["skips"] + 1
        title = self._title(item)
        self.db.event(item["id"], "skipped", {"title": title, "skips": skips})
        if skips >= int(self.cfg["max_skips"]):
            mp4, js = move_pair(item["mp4"], item["json"], Path(self.cfg["paths"]["skipped"]))
            media.drop_previews(item["mp4"], self.cfg)
            try:
                data = read_json(js)
                data["status"] = "skipped"
                write_json(js, data)
            except Exception:  # noqa: BLE001
                pass
            self.db.update_item(item["id"], status="skipped", skips=skips, mp4=mp4, json=js)
            self.db.event(item["id"], "moved_to_skipped", {"title": title})
            self.app.bot.refresh(item["id"])
            self.app.bot.send(f"🗑 Шортс #{item['id']} «{title}» пропущено {skips} рази — "
                              f"файли перенесено в skipped/.")
            log.info("#%s → skipped/", item["id"])
        else:
            self.db.update_item(item["id"], status="queued", skips=skips, qpos=self.db.next_qpos())
            self.app.bot.refresh(item["id"])
            self.db.update_item(item["id"], slot_at=None)
            log.info("#%s пропущено (%s/%s) → у кінець черги", item["id"], skips, self.cfg["max_skips"])

    # ---------- сповіщення за годину ----------
    def process_notifications(self, now: int) -> None:
        lead = int(self.cfg["notify_before_minutes"]) * 60
        min_lead = int(self.cfg["min_notify_lead_minutes"]) * 60
        for s in self.db.q("SELECT * FROM slots WHERE state='pending' AND slot_at>? AND slot_at<=? "
                           "ORDER BY slot_at", (now + min_lead, now + lead)):
            item = self._next_item()
            if not item:
                return
            with self.db.tx():
                self.db.conn.execute("UPDATE slots SET item_id=?, state='notified' WHERE slot_at=?",
                                     (item["id"], s["slot_at"]))
                self.db.conn.execute("UPDATE items SET status='notified', slot_at=? WHERE id=?",
                                     (s["slot_at"], item["id"]))
            try:
                self.app.bot.notify_item(item["id"])
            except Exception as e:  # noqa: BLE001
                log.error("Не вдалося надіслати сповіщення #%s: %s — спробую пізніше", item["id"], e)
                with self.db.tx():
                    self.db.conn.execute("UPDATE slots SET item_id=NULL, state='pending' WHERE slot_at=?",
                                         (s["slot_at"],))
                    self.db.conn.execute("UPDATE items SET status='queued', slot_at=NULL WHERE id=?", (item["id"],))
                return
            self.db.event(item["id"], "notified", {"slot": s["slot_at"]})
            log.info("Сповіщення: #%s на слот %s", item["id"], fmt_slot(self.cfg, s["slot_at"]))

    def _next_item(self) -> dict | None:
        for item in self.db.queue():
            if Path(item["mp4"]).exists() and Path(item["json"]).exists():
                return item
            log.warning("#%s: файл зник (%s) — прибираю з черги", item["id"], item["mp4"])
            self.db.update_item(item["id"], status="missing")
            self.db.event(item["id"], "missing", {"file": Path(item["mp4"]).name})
        return None

    # ---------- звіти ----------
    def maybe_daily_report(self, now: int) -> None:
        dt = local_dt(self.cfg, now)
        if dt.strftime("%H:%M") < self.cfg["reports"]["daily_time"]:
            return
        day = dt.strftime("%Y-%m-%d")
        if self.db.get("daily_report_day") == day:
            return
        self.db.set("daily_report_day", day)
        from reports import daily_report
        self.app.bot.send(daily_report(self.app, now), html_mode=True)

    def maybe_stats(self, now: int) -> None:
        if self._stats_thread and self._stats_thread.is_alive():
            return
        hours = float(self.cfg["reports"]["stats_after_hours"])
        due = self.db.items("status='posted' AND stats_sent=0 AND posted_at<=?", (now - int(hours * 3600),),
                            order="posted_at")
        if not due:
            return
        from reports import send_stats

        def work():
            for item in due[:5]:
                send_stats(self.app, item)
        if getattr(self.app.publisher, "sync", False):
            work()
        else:
            self._stats_thread = threading.Thread(target=work, daemon=True, name="stats")
            self._stats_thread.start()

    def _title(self, item: dict) -> str:
        try:
            return load_meta(item["json"], item["title_override"]).yt_title
        except Exception:  # noqa: BLE001
            return Path(item["mp4"]).stem
