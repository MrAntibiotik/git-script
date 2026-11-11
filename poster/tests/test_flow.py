"""Наскрізні тести логіки (без мережі): черга → сповіщення → підтвердження → публікація → архів → звіт.

Запуск:  python -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
from bot import Bot  # noqa: E402
from config import load_config  # noqa: E402
from poster import App  # noqa: E402


class FakeTG:
    def __init__(self):
        self.sent, self.edits, self.answers, self.msg_id = [], [], [], 100

    def _msg(self):
        self.msg_id += 1
        return {"message_id": self.msg_id}

    def send_video(self, chat, path, caption, kb=None, **kw):
        self.sent.append(("video", caption, kb, path))
        return self._msg()

    def send_message(self, chat, text, reply_markup=None, reply_to=None, **kw):
        self.sent.append(("text", text, reply_markup, reply_to))
        return self._msg()

    def edit_caption(self, chat, mid, caption, kb=None):
        self.edits.append((mid, caption, kb))

    def edit_text(self, chat, mid, text, kb=None):
        self.edits.append((mid, text, kb))

    def answer_callback(self, cid, text="", alert=False):
        self.answers.append(text)


class FlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        cfg = {
            "slots": ["10:00", "13:00", "16:00", "19:00", "21:00"],
            "dry_run": True,
            "retry": {"attempts": 3, "pause_seconds": 0},
            "telegram": {"bot_token": "123:abc", "chat_id": "42"},
            "reports": {"daily_time": "22:30", "stats_after_hours": 24},
        }
        (self.tmp / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
        for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
            os.environ.pop(k, None)
        self.cfg = load_config(self.tmp / "config.json")
        self.app = self._app()
        self.tz = self.cfg["tz"]

    def _app(self):
        app = App.__new__(App)
        App.__init__(app, self.cfg, bot=object())
        bot = Bot(app)
        bot.tg = FakeTG()
        app.bot = bot
        app.publisher.sync = True
        app.publisher.sleep = lambda s: None
        real_tick = app.scheduler.tick

        def tick(now):
            config.FAKE_NOW = now
            real_tick(now)
        app.scheduler.tick = tick
        return app

    def tearDown(self):
        config.FAKE_NOW = None
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def ts(self, d, hm):
        h, m = map(int, hm.split(":"))
        return int(datetime(2026, 11, d, h, m, tzinfo=self.tz).timestamp())

    def make_short(self, name, game="WoT", age=120):
        d = self.cfg["paths"]["ready"] / game
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{name}.mp4").write_bytes(b"\x00" * 1024)
        (d / f"{name}.json").write_text(json.dumps({
            "status": "ready_to_post",
            "youtube": {"title": f"Титул {name}", "description": "Опис", "tags": ["wot", "танки"],
                        "hashtags": ["#wot", "танки"]},
            "tiktok": {"caption": "Текст для TikTok #wot"},
            "instagram": {"caption": "Текст для Instagram"},
        }, ensure_ascii=False), encoding="utf-8")
        old = time.time() - age
        os.utime(d / f"{name}.mp4", (old, old))
        os.utime(d / f"{name}.json", (old, old))
        return d / f"{name}.mp4"

    def press(self, data, item):
        self.app.bot.on_callback({"id": "cb", "data": data, "message": {"message_id": item["tg_msg"],
                                                                         "chat": {"id": 42}}})

    # ------------------------------------------------------------------
    def test_full_path(self):
        self.make_short("a")
        self.make_short("b", game="Dota")
        tg = self.app.bot.tg
        sch = self.app.scheduler

        sch.tick(self.ts(11, "11:00"))  # 12:00-сповіщення ще ні (13:00 - 60 хв = 12:00)
        self.assertEqual(len(self.app.db.queue()), 2)
        self.assertFalse(any(s[0] == "video" for s in tg.sent))

        sch.tick(self.ts(11, "12:00"))
        vids = [s for s in tg.sent if s[0] == "video"]
        self.assertEqual(len(vids), 1)
        cap = vids[0][1]
        self.assertIn("🎬 <b>Титул a</b>", cap)
        self.assertIn("#wot #танки", cap)
        self.assertIn("🕐 Слот: 13:00 | Платформи: YT ✅ TT ✅ IG ✅ FB ✅", cap)
        texts = tg.sent[-1]
        self.assertIn("Текст для TikTok", texts[1])
        self.assertIn("Текст для Instagram", texts[1])

        item = self.app.db.items("status='notified'")[0]
        self.press(f"tog:{item['id']}:facebook", item)
        self.press(f"title:{item['id']}", item)
        self.app.bot.on_message({"text": "Нова назва", "chat": {"id": 42}})
        self.press(f"pub:{item['id']}", item)
        item = self.app.db.get_item(item["id"])
        self.assertEqual(item["status"], "approved")
        self.assertEqual(item["disabled"], ["facebook"])
        self.assertEqual(item["title_override"], "Нова назва")

        sch.tick(self.ts(11, "13:00"))
        item = self.app.db.get_item(item["id"])
        self.assertEqual(item["status"], "posted")
        self.assertIn(str(self.cfg["paths"]["posted"] / "2026-11-11"), item["mp4"])
        self.assertTrue(Path(item["mp4"]).exists())
        data = json.loads(Path(item["json"]).read_text(encoding="utf-8"))
        self.assertEqual(data["status"], "posted")
        self.assertEqual(data["posting"]["platforms"]["youtube"]["status"], "ok")
        self.assertEqual(data["posting"]["platforms"]["facebook"]["status"], "disabled")
        self.assertNotIn("facebook", item["results"])
        last_cap = tg.edits[-1][1]
        self.assertIn("✅ Опубліковано", last_cap)
        self.assertIn("✏️ Змінено: назву", last_cap)
        self.assertIn("Нова назва", last_cap)

        # Звіт
        sch.tick(self.ts(11, "22:31"))
        report = [s for s in tg.sent if s[0] == "text" and "Підсумок" in s[1]]
        self.assertEqual(len(report), 1)
        self.assertIn("Опубліковано: 1", report[0][1])

        # Перегляди через 24 год
        sch.tick(self.ts(12, "13:05"))
        self.assertTrue(any("Перегляди за 24 год" in s[1] for s in tg.sent if s[0] == "text"))

    def test_skip_three_times_and_restart(self):
        self.make_short("a")
        sch = self.app.scheduler
        sch.tick(self.ts(11, "12:00"))
        item = self.app.db.items("status='notified'")[0]
        # «Перезапуск ПК»: новий App з тією ж БД
        self.app = self._app()
        sch = self.app.scheduler
        self.assertEqual(self.app.db.get_item(item["id"])["status"], "notified")
        sch.tick(self.ts(11, "13:00"))  # без підтвердження → пропуск 1
        it = self.app.db.get_item(item["id"])
        self.assertEqual((it["status"], it["skips"]), ("queued", 1))
        sch.tick(self.ts(11, "15:00"))  # знову сповіщення на 16:00
        sch.tick(self.ts(11, "16:00"))
        sch.tick(self.ts(11, "18:00"))
        sch.tick(self.ts(11, "19:00"))
        it = self.app.db.get_item(item["id"])
        self.assertEqual(it["status"], "skipped")
        self.assertIn(str(self.cfg["paths"]["skipped"]), it["mp4"])
        self.assertTrue(any("пропущено 3 рази" in s[1] for s in self.app.bot.tg.sent if s[0] == "text"))

    def test_fifo_and_overflow(self):
        for n in "abcdefg":
            self.make_short(n, age=1000 - ord(n))
        sch = self.app.scheduler
        order = []
        for hm in ("10:00", "13:00", "16:00", "19:00", "21:00"):
            h, m = map(int, hm.split(":"))
            sch.tick(self.ts(11, f"{h - 1:02d}:{m:02d}"))
            it = self.app.db.items("status='notified'")[0]
            order.append(Path(it["mp4"]).stem)
            self.press(f"pub:{it['id']}", it)
            sch.tick(self.ts(11, hm))
        self.assertEqual(order, list("abcde"))
        self.assertEqual([Path(i["mp4"]).stem for i in self.app.db.queue()], ["f", "g"])

    def test_platform_failure_is_independent(self):
        self.make_short("a")
        self.cfg["dry_run"] = False

        class Boom:
            reauth_hint = "x"

            def publish(self, *a):
                raise RuntimeError("мережа впала")

        class Ok:
            def publish(self, path, meta):
                return {"id": "1", "url": "https://ok"}

        from platforms import TokenExpired

        class Expired:
            reauth_hint = "python poster.py auth tiktok"

            def publish(self, *a):
                raise TokenExpired("протерміновано")

        calls = {"n": 0}

        class Flaky:
            def publish(self, path, meta):
                calls["n"] += 1
                if calls["n"] < 3:
                    raise RuntimeError("тимчасово")
                return {"id": "2", "url": "https://flaky"}

        self.app.platforms = {"youtube": Ok(), "tiktok": Expired(), "instagram": Boom(), "facebook": Flaky()}
        sch = self.app.scheduler
        sch.tick(self.ts(11, "12:00"))
        it = self.app.db.items("status='notified'")[0]
        self.press(f"pub:{it['id']}", it)
        sch.tick(self.ts(11, "13:00"))
        it = self.app.db.get_item(it["id"])
        self.assertEqual(it["status"], "posted")
        r = it["results"]
        self.assertEqual(r["youtube"]["status"], "ok")
        self.assertEqual(r["facebook"]["status"], "ok")
        self.assertEqual(r["facebook"]["attempts"], 3)
        self.assertEqual(r["instagram"]["status"], "error")
        self.assertEqual(r["instagram"]["attempts"], 3)
        self.assertTrue(r["tiktok"]["reauth"])
        msgs = [s[1] for s in self.app.bot.tg.sent if s[0] == "text"]
        self.assertTrue(any("Потрібна повторна авторизація: TikTok" in m for m in msgs))
        self.assertTrue(any("Instagram: не вдалося" in m and "мережа впала" in m for m in msgs))

    def test_health_check(self):
        from platforms import TokenExpired

        class Good:
            def check(self):
                return "@ok"

            def token_warning(self):
                return "⏳ Refresh-токен TikTok спливає через 3 дн."

        class Bad:
            reauth_hint = "python poster.py auth youtube"

            def check(self):
                raise TokenExpired("invalid_grant")

            def token_warning(self):
                return None

        self.cfg["dry_run"] = False
        self.app.platforms = {"youtube": Bad(), "tiktok": Good()}
        self.app.scheduler.tick(self.ts(11, "02:00"))  # перевірка при запуску
        msgs = [s[1] for s in self.app.bot.tg.sent if s[0] == "text"]
        self.assertTrue(any("Потрібна повторна авторизація: YouTube" in m for m in msgs))
        self.assertTrue(any("спливає через 3 дн" in m for m in msgs))
        n = len(self.app.bot.tg.sent)
        self.app.scheduler.tick(self.ts(11, "05:00"))  # до 09:00 повторно не перевіряє
        self.assertEqual(len(self.app.bot.tg.sent), n)
        self.app.scheduler.tick(self.ts(11, "09:10"))  # щоденна перевірка: нагадування про авторизацію
        new = [s[1] for s in self.app.bot.tg.sent[n:]]  # (раз на 6 год), попередження TikTok — раз на день
        self.assertEqual(len(new), 1)
        self.assertIn("YouTube", new[0])
        self.assertIn("🔑", self.app.status_text())

    def test_missed_slot_while_pc_off(self):
        self.make_short("a")
        sch = self.app.scheduler
        sch.tick(self.ts(11, "12:00"))
        it = self.app.db.items("status='notified'")[0]
        self.press(f"pub:{it['id']}", it)
        sch.tick(self.ts(11, "18:00"))  # ПК «спав» 5 годин
        it = self.app.db.get_item(it["id"])
        # без штрафу повернуто на початок черги і одразу призначено на найближчий слот 19:00
        self.assertEqual((it["status"], it["skips"]), ("notified", 0))
        self.assertEqual(it["slot_at"], self.ts(11, "19:00"))
        self.assertEqual(self.app.db.slot(self.ts(11, "13:00"))["state"], "missed")


if __name__ == "__main__":
    unittest.main()
