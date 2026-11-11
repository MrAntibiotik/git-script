"""Тести читання json від generate_meta.py і формування підписів Telegram."""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bot import CAPTION_LIMIT, visible_len  # noqa: E402
from meta import load_meta, move_pair  # noqa: E402
from tests.test_flow import FakeTG, FlowTest  # noqa: E402


class MetaTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, data, name="x"):
        p = self.tmp / "WoT" / f"{name}.json"
        p.parent.mkdir(exist_ok=True)
        p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8-sig")
        return p

    def test_full_format(self):
        m = load_meta(self.write({
            "youtube": {"title": "Назва", "description": "Опис", "tags": "wot, танки", "hashtags": "wot танки"},
            "tiktok": {"caption": "TT"}, "instagram": {"caption": "IG #свій"}, "facebook": {"text": "FB"}}))
        self.assertEqual(m.yt_title, "Назва")
        self.assertEqual(m.yt_tags, ["wot", "танки"])
        self.assertEqual(m.hashtags, ["#wot", "#танки"])
        self.assertEqual(m.tiktok_text, "TT\n\n#wot #танки")
        self.assertEqual(m.instagram_text, "IG #свій")  # власні хештеги не дублюються
        self.assertEqual(m.facebook_text, "FB\n\n#wot #танки")
        self.assertEqual(m.game, "WoT")

    def test_minimal_and_strings(self):
        m = load_meta(self.write({"title": "Просто назва #wot", "tiktok": "текст TT", "instagram": "текст IG"}))
        self.assertEqual(m.yt_title, "Просто назва #wot")
        self.assertEqual(m.hashtags, ["#wot"])
        self.assertEqual(m.facebook_text, m.instagram_text)

    def test_empty_falls_back_to_filename(self):
        m = load_meta(self.write({}, name="clip_42"))
        self.assertEqual(m.yt_title, "clip_42")
        self.assertEqual(m.tiktok_text, "clip_42")

    def test_title_override(self):
        m = load_meta(self.write({"youtube": {"title": "A"}}), "  B  ")
        self.assertEqual(m.yt_title, "B")

    def test_move_pair_collision(self):
        for d in ("src", "dst"):
            (self.tmp / d).mkdir()
            (self.tmp / d / "a.mp4").write_bytes(b"1")
            (self.tmp / d / "a.json").write_text("{}")
        mp4, js = move_pair(str(self.tmp / "src/a.mp4"), str(self.tmp / "src/a.json"), self.tmp / "dst")
        self.assertEqual(Path(mp4).name, "a_2.mp4")
        self.assertEqual(Path(js).name, "a_2.json")
        self.assertFalse((self.tmp / "src/a.mp4").exists())


class CaptionTest(FlowTest):
    """Використовує оточення FlowTest (App + FakeTG)."""

    def test_caption_limit(self):
        mp4 = self.make_short("long")
        js = mp4.with_suffix(".json")
        data = json.loads(js.read_text(encoding="utf-8"))
        data["youtube"]["title"] = "Дуже довга назва " * 10
        data["youtube"]["hashtags"] = [f"тег{i}" for i in range(300)]
        js.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.utime(js, (mp4.stat().st_mtime,) * 2)  # інакше сканер вважає файл «ще записується»
        self.app.scheduler.tick(self.ts(11, "12:00"))
        cap = [s for s in self.app.bot.tg.sent if s[0] == "video"][0][1]
        self.assertLessEqual(visible_len(cap), CAPTION_LIMIT)
        self.assertIn("🕐 Слот: 13:00", cap)
        self.assertIn("#тег0", cap)

    def test_video_send_fails_falls_back_to_text(self):
        from telegram_api import TelegramError

        class BrokenVideo(FakeTG):
            def send_video(self, *a, **k):
                raise TelegramError("sendVideo: Request Entity Too Large")

        self.app.bot.tg = BrokenVideo()
        self.make_short("a")
        self.app.scheduler.tick(self.ts(11, "12:00"))
        it = self.app.db.items("status='notified'")[0]
        self.assertEqual(it["tg_msg_is_text"], 1)
        self.press(f"pub:{it['id']}", it)
        self.assertEqual(self.app.db.get_item(it["id"])["status"], "approved")

    def test_foreign_chat_ignored(self):
        self.make_short("a")
        self.app.scheduler.tick(self.ts(11, "12:00"))
        it = self.app.db.items("status='notified'")[0]
        self.app.bot.handle({"update_id": 1, "callback_query": {
            "id": "x", "data": f"pub:{it['id']}", "message": {"message_id": it["tg_msg"], "chat": {"id": 666}}}})
        self.assertEqual(self.app.db.get_item(it["id"])["status"], "notified")

    def test_stale_button_after_skip(self):
        self.make_short("a")
        self.app.scheduler.tick(self.ts(11, "12:00"))
        it = self.app.db.items("status='notified'")[0]
        self.app.scheduler.tick(self.ts(11, "13:00"))  # пропуск
        self.press(f"pub:{it['id']}", it)
        self.assertEqual(self.app.db.get_item(it["id"])["status"], "queued")
        self.assertIn("неактуальне", self.app.bot.tg.answers[-1])


del FlowTest  # щоб тести FlowTest не запускались двічі з цього модуля

if __name__ == "__main__":
    unittest.main()
