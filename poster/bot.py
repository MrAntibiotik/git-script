"""Telegram-бот: сповіщення-відео з кнопками підтвердження, команди, службові повідомлення.

Бот відповідає ЛИШЕ на chat_id з налаштувань.
"""
from __future__ import annotations

import html
import logging
import re
import threading
from pathlib import Path

import media
from config import NAMES, PLATFORMS, SHORT, enabled_platforms, fmt_slot, now_ts
from meta import PostMeta, load_meta
from telegram_api import TelegramAPI, TelegramError

log = logging.getLogger("bot")

esc = html.escape
CAPTION_LIMIT = 1024

HELP = (
    "🤖 <b>poster</b> — автопублікація шортсів\n\n"
    "/queue — черга\n"
    "/status — найближчі слоти\n"
    "/report — підсумок за сьогодні\n"
    "/retry N — повторити невдалі платформи для шортса #N\n"
    "/testslot [хв] — позаплановий слот через N хв (типово 10) для тесту\n"
    "/cancel — скасувати введення назви"
)


def visible_len(text: str) -> int:
    return len(html.unescape(re.sub(r"<[^>]+>", "", text)))


class Bot:
    def __init__(self, app):
        self.app = app
        self.cfg = app.cfg
        self.db = app.db
        self.tg = TelegramAPI(self.cfg["secrets"]["TELEGRAM_BOT_TOKEN"])
        chat = self.cfg["secrets"]["TELEGRAM_CHAT_ID"]
        if not chat:
            raise TelegramError("Не задано TELEGRAM_CHAT_ID")
        self.chat_id = int(chat)

    # ================= службові повідомлення =================
    def send(self, text: str, html_mode: bool = False) -> None:
        try:
            self.tg.send_message(self.chat_id, text if html_mode else esc(text))
        except TelegramError as e:
            log.error("Не вдалося надіслати повідомлення в Telegram: %s", e)

    def notify_reauth(self, key: str, reason: str) -> None:
        k = f"reauth_{key}"
        last = int(self.db.get(k) or 0)
        if now_ts() - last < 6 * 3600:
            return
        self.db.set(k, now_ts())
        hint = self.app.platforms[key].reauth_hint if key in self.app.platforms else f"python poster.py auth {key}"
        self.send(f"🔑 <b>Потрібна повторна авторизація: {NAMES[key]}</b>\n"
                  f"Причина: {esc(reason[:300])}\n\nНа ПК виконайте:\n<code>{esc(hint)}</code>", html_mode=True)

    # ================= сповіщення про шортс =================
    def _meta(self, item: dict) -> PostMeta:
        try:
            return load_meta(item["json"], item["title_override"])
        except Exception as e:  # noqa: BLE001
            log.warning("Метадані #%s недоступні: %s", item["id"], e)
            return PostMeta(yt_title=item["title_override"] or Path(item["mp4"]).stem)

    def _platform_line(self, item: dict) -> str:
        parts = []
        for p in enabled_platforms(self.cfg):
            parts.append(f"{SHORT[p]} {'🚫' if p in item['disabled'] else '✅'}")
        return " ".join(parts) or "—"

    def _status_block(self, item: dict) -> str:
        st = item["status"]
        slot = fmt_slot(self.cfg, item["slot_at"]) if item["slot_at"] else "—"
        edited = f"\n✏️ Змінено: {', '.join(item['edited'])}" if item["edited"] else ""
        if st == "notified":
            return f"⏳ Очікує підтвердження до {slot}{edited}"
        if st == "approved":
            return f"👍 Підтверджено — публікація о {slot}{edited}"
        if st == "publishing":
            return "⏫ Публікується…" + self._results(item)
        if st == "posted":
            return f"✅ Опубліковано{edited}" + self._results(item)
        if st == "failed":
            return "❌ Не опубліковано" + self._results(item) + f"\nПовторити: /retry {item['id']}"
        if st == "queued":
            if item["note"]:
                return esc(item["note"])
            return f"⏭ Пропущено ({item['skips']}/{self.cfg['max_skips']}) — шортс переміщено в кінець черги"
        if st == "skipped":
            return f"⏭ Пропущено {item['skips']} рази — файли перенесено в skipped/"
        if st == "missing":
            return "⚠️ Файл не знайдено — шортс прибрано з черги"
        return st

    def _results(self, item: dict) -> str:
        lines = []
        for p in PLATFORMS:
            r = item["results"].get(p)
            if p in item["disabled"]:
                if p in self.app.platforms:
                    lines.append(f"{SHORT[p]}: 🚫 вимкнено")
                continue
            if not r:
                continue
            if r.get("status") == "ok":
                lock = " 🔒 приватно" if r.get("privacy") == "SELF_ONLY" else ""
                link = f'<a href="{esc(r["url"])}">{NAMES[p]}</a>' if r.get("url") else f"{NAMES[p]} ✅"
                lines.append(f"{SHORT[p]}: {link}{lock}")
            else:
                lines.append(f"{SHORT[p]}: ❌ {esc(str(r.get('error', ''))[:120])}")
        return ("\n" + "\n".join(lines)) if lines else ""

    def caption(self, item: dict, meta: PostMeta | None = None) -> str:
        meta = meta or self._meta(item)
        slot = fmt_slot(self.cfg, item["slot_at"]) if item["slot_at"] else "—"
        head = f"🎬 <b>{esc(meta.yt_title)}</b>"
        tags = esc(" ".join(meta.hashtags))
        tail = f"🕐 Слот: {slot} | Платформи: {self._platform_line(item)}\n\n{self._status_block(item)}"
        text = "\n".join(x for x in (head, tags, tail) if x)
        if visible_len(text) > CAPTION_LIMIT and tags:
            room = CAPTION_LIMIT - visible_len(head + "\n\n" + tail) - 2
            short = " ".join(meta.hashtags)[:max(0, room)].rsplit(" ", 1)[0] if room > 0 else ""
            text = "\n".join(x for x in (head, esc(short), tail) if x)
        if visible_len(text) > CAPTION_LIMIT:
            text = text[:CAPTION_LIMIT - 1] + "…"
            text = re.sub(r"<[^>]*$", "", text)
        return text

    def texts(self, item: dict, meta: PostMeta | None = None) -> str:
        meta = meta or self._meta(item)
        private = "" if self.cfg["platforms"]["tiktok"].get("public") else " (приватно)"
        parts = [f"📝 Тексти для шортса #{item['id']}"]
        if "tiktok" in self.app.platforms:
            parts.append(f"🎵 <b>TikTok</b>{private}:\n{esc(meta.tiktok_text[:1300])}")
        same = meta.facebook_text == meta.instagram_text
        if "instagram" in self.app.platforms or "facebook" in self.app.platforms:
            parts.append(f"📸 <b>Instagram{' + Facebook' if same else ''}</b>:\n{esc(meta.instagram_text[:1300])}")
        if not same and "facebook" in self.app.platforms:
            parts.append(f"📘 <b>Facebook</b>:\n{esc(meta.facebook_text[:1000])}")
        return "\n\n".join(parts)[:4096]

    def keyboard(self, item: dict) -> dict | None:
        if item["status"] not in ("notified", "approved"):
            return None
        i = item["id"]
        first = ({"text": "🚀 Опублікувати", "callback_data": f"pub:{i}"} if item["status"] == "notified"
                 else {"text": "↩️ Скасувати підтвердження", "callback_data": f"unpub:{i}"})
        toggles = [{"text": f"{SHORT[p]} {'🚫' if p in item['disabled'] else '✅'}",
                    "callback_data": f"tog:{i}:{p}"} for p in enabled_platforms(self.cfg)]
        rows = [[first],
                [{"text": "🔄 Перегенерувати тексти", "callback_data": f"regen:{i}"}],
                [{"text": "✏️ Ввести назву вручну", "callback_data": f"title:{i}"}]]
        if toggles:
            rows.append([{"text": "Вимкнути платформу:", "callback_data": f"noop:{i}"}])
            rows.append(toggles)
        return {"inline_keyboard": rows}

    def notify_item(self, item_id: int) -> None:
        """Надсилає сам шортс (відеоповідомлення) + другим повідомленням тексти."""
        item = self.db.get_item(item_id)
        meta = self._meta(item)
        caption, kb = self.caption(item, meta), self.keyboard(item)
        is_text = 0
        try:
            video = media.telegram_copy(item["mp4"], self.cfg)
            info = media.probe(video, self.cfg["ffprobe"])
            msg = self.tg.send_video(self.chat_id, video, caption, kb, width=info.get("width"),
                                     height=info.get("height"), duration=info.get("duration"))
        except (media.MediaError, TelegramError, OSError) as e:
            log.error("Не вдалося надіслати відео #%s: %s", item_id, e)
            msg = self.tg.send_message(self.chat_id, caption, reply_markup=kb)
            self.send(f"⚠️ Відео #{item_id} не вдалося надіслати в чат: {e}")
            is_text = 1
        text_msg = self.tg.send_message(self.chat_id, self.texts(item, meta), reply_to=msg["message_id"])
        self.db.update_item(item_id, tg_msg=msg["message_id"], tg_msg_is_text=is_text,
                            tg_text_msg=text_msg["message_id"] if text_msg else None)

    def refresh(self, item_id: int, texts: bool = False) -> None:
        item = self.db.get_item(item_id)
        if not item or not item["tg_msg"]:
            return
        meta = self._meta(item)
        try:
            cap, kb = self.caption(item, meta), self.keyboard(item)
            if item["tg_msg_is_text"]:
                self.tg.edit_text(self.chat_id, item["tg_msg"], cap, kb)
            else:
                self.tg.edit_caption(self.chat_id, item["tg_msg"], cap, kb)
            if texts and item["tg_text_msg"]:
                self.tg.edit_text(self.chat_id, item["tg_text_msg"], self.texts(item, meta))
        except TelegramError as e:
            log.warning("Не вдалося оновити повідомлення #%s: %s", item_id, e)

    # ================= вхідні оновлення =================
    def poll_once(self, timeout: int = 15) -> None:
        offset = self.db.get("tg_offset")
        updates = self.tg.get_updates(int(offset) if offset else None, timeout)
        for u in updates:
            self.db.set("tg_offset", u["update_id"] + 1)
            try:
                self.handle(u)
            except Exception:  # noqa: BLE001
                log.exception("Помилка обробки оновлення %s", u.get("update_id"))

    def handle(self, u: dict) -> None:
        if "callback_query" in u:
            cq = u["callback_query"]
            chat = ((cq.get("message") or {}).get("chat") or {}).get("id")
            if chat != self.chat_id:
                log.warning("Callback від чужого chat_id=%s — ігнорую", chat)
                self.tg.answer_callback(cq["id"], "⛔")
                return
            self.on_callback(cq)
        elif "message" in u:
            m = u["message"]
            chat = (m.get("chat") or {}).get("id")
            if chat != self.chat_id:
                log.warning("Повідомлення від невідомого chat_id=%s (%s) — ігнорую",
                            chat, (m.get("from") or {}).get("username"))
                return
            self.on_message(m)

    def on_callback(self, cq: dict) -> None:
        answer = lambda text="", alert=False: self.tg.answer_callback(cq["id"], text, alert)  # noqa: E731
        try:
            action, sid, *rest = (cq.get("data") or "").split(":")
            item_id = int(sid)
        except ValueError:
            return answer()
        if action == "noop":
            return answer("Натисніть на платформу нижче, щоб вимкнути/увімкнути її")
        item = self.db.get_item(item_id)
        if (not item or item["status"] not in ("notified", "approved")
                or cq["message"]["message_id"] != item["tg_msg"]):
            return answer("Це сповіщення вже неактуальне", True)

        if action == "pub":
            if not self.app.publisher.targets(item):
                return answer("Усі платформи вимкнено — нічого публікувати", True)
            if item["status"] != "notified":
                return answer()
            self.db.update_item(item_id, status="approved")
            self.db.event(item_id, "approved")
            if self.cfg.get("publish_on_approve"):
                answer("Публікую зараз…")
                self.app.publisher.start(item_id)
            else:
                answer(f"Підтверджено. Публікація о {fmt_slot(self.cfg, item['slot_at'])}")
                self.refresh(item_id)
        elif action == "unpub":
            if item["status"] == "approved" and not self.app.publisher.is_active(item_id):
                self.db.update_item(item_id, status="notified")
                self.refresh(item_id)
            answer("Підтвердження скасовано")
        elif action == "tog" and rest and rest[0] in PLATFORMS:
            p = rest[0]
            disabled = [x for x in item["disabled"] if x != p] if p in item["disabled"] else item["disabled"] + [p]
            self.db.update_item(item_id, disabled=disabled)
            self.refresh(item_id)
            answer(f"{NAMES[p]}: {'вимкнено' if p in disabled else 'увімкнено'}")
        elif action == "title":
            self.db.set("await_title", item_id)
            self.tg.send_message(self.chat_id, f"✏️ Надішліть нову назву для YouTube (шортс #{item_id}, "
                                               "до 100 символів). /cancel — скасувати",
                                 reply_markup={"force_reply": True, "input_field_placeholder": "Нова назва"})
            answer()
        elif action == "regen":
            if not self.cfg.get("regenerate_command"):
                return answer("Не налаштовано regenerate_command у config.json", True)
            answer("Перегенеровую тексти…")
            threading.Thread(target=self._regen, args=(item_id,), daemon=True).start()
        else:
            answer()

    def _regen(self, item_id: int) -> None:
        item = self.db.get_item(item_id)
        args = [str(a).format(mp4=item["mp4"], json=item["json"], game=item["game"])
                for a in self.cfg["regenerate_command"]]
        log.info("Перегенерація текстів #%s: %s", item_id, args)
        try:
            r = media.run_hidden(args, timeout=600, cwd=self.cfg.get("regenerate_cwd") or None)
            if r.returncode != 0:
                raise RuntimeError((r.stderr or r.stdout or b"").decode(errors="ignore")[-400:] or f"код {r.returncode}")
            load_meta(item["json"])
        except Exception as e:  # noqa: BLE001
            log.error("Перегенерація #%s не вдалася: %s", item_id, e)
            self.send(f"❌ Перегенерація текстів #{item_id} не вдалася: {e}")
            return
        item = self.db.get_item(item_id)
        edited = [x for x in item["edited"] if x not in ("назву", "тексти")] + ["тексти"]
        self.db.update_item(item_id, title_override=None, edited=edited)
        self.db.event(item_id, "edited", "regen")
        self.refresh(item_id, texts=True)

    def on_message(self, m: dict) -> None:
        text = (m.get("text") or "").strip()
        if not text:
            return
        if text.startswith("/"):
            cmd, *args = text.split()
            return self.command(cmd.split("@")[0].lower(), args)
        await_id = self.db.get("await_title")
        if await_id:
            item_id = int(await_id)
            self.db.set("await_title", None)
            item = self.db.get_item(item_id)
            if not item or item["status"] not in ("notified", "approved"):
                return self.send("Цей шортс уже неактуальний — назву не змінено.")
            title = re.sub(r"\s+", " ", text)[:100]
            edited = [x for x in item["edited"] if x != "назву"] + ["назву"]
            self.db.update_item(item_id, title_override=title, edited=edited)
            self.db.event(item_id, "edited", {"title": title})
            self.refresh(item_id)
            note = " (обрізано до 100 символів)" if len(text) > 100 else ""
            return self.send(f"✅ Назву для #{item_id} змінено{note}: {title}")
        self.send(HELP, html_mode=True)

    def command(self, cmd: str, args: list[str]) -> None:
        app = self.app
        if cmd in ("/start", "/help"):
            self.send(HELP, html_mode=True)
        elif cmd == "/cancel":
            self.db.set("await_title", None)
            self.send("Скасовано.")
        elif cmd == "/queue":
            self.send(app.queue_text(), html_mode=True)
        elif cmd == "/status":
            self.send(app.status_text(), html_mode=True)
        elif cmd == "/report":
            from reports import daily_report
            self.send(daily_report(app), html_mode=True)
        elif cmd == "/retry":
            if not args or not args[0].lstrip("#").isdigit():
                return self.send("Використання: /retry N")
            item_id = int(args[0].lstrip("#"))
            item = self.db.get_item(item_id)
            if not item or item["status"] not in ("failed", "posted"):
                return self.send("Повторити можна лише шортс зі статусом «не опубліковано» або частково опублікований.")
            pending = [p for p in app.publisher.targets(item) if item["results"].get(p, {}).get("status") != "ok"]
            if not pending:
                return self.send("Усі платформи для цього шортса вже успішні.")
            self.send(f"🔁 Повторюю #{item_id}: {', '.join(NAMES[p] for p in pending)}")
            app.publisher.start(item_id)
        elif cmd == "/testslot":
            minutes = int(args[0]) if args and args[0].isdigit() else 10
            ts = app.scheduler.add_manual_slot(minutes)
            self.send(f"🧪 Додано позаплановий слот о {fmt_slot(self.cfg, ts)}. "
                      "Сповіщення прийде протягом хвилини (якщо черга не порожня).")
        else:
            self.send(HELP, html_mode=True)
