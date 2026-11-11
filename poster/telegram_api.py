"""Мінімальний клієнт Telegram Bot API (requests, без сторонніх бібліотек)."""
from __future__ import annotations

import json
import logging
import threading
import time

import requests

log = logging.getLogger("telegram")


class TelegramError(Exception):
    pass


class TelegramAPI:
    def __init__(self, token: str):
        if not token:
            raise TelegramError("Не задано TELEGRAM_BOT_TOKEN")
        self.token = token
        self.base = f"https://api.telegram.org/bot{token}/"
        self._local = threading.local()

    def _session(self) -> requests.Session:
        s = getattr(self._local, "s", None)
        if s is None:
            s = self._local.s = requests.Session()
        return s

    def _clean(self, text: str) -> str:
        return str(text).replace(self.token, "***")

    def call(self, method: str, files: dict | None = None, http_timeout: int = 60, **params):
        data = {k: (json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v)
                for k, v in params.items() if v is not None}
        last = None
        for attempt in range(5):
            handles = {}
            try:
                if files:
                    handles = {k: open(p, "rb") for k, p in files.items()}
                r = self._session().post(self.base + method, data=data, files=handles or None,
                                         timeout=http_timeout)
            except requests.RequestException as e:
                last = TelegramError(f"{method}: мережа: {self._clean(e)}")
                time.sleep(min(30, 2 ** attempt))
                continue
            finally:
                for h in handles.values():
                    h.close()
            try:
                j = r.json()
            except ValueError:
                last = TelegramError(f"{method}: HTTP {r.status_code}")
                time.sleep(min(30, 2 ** attempt))
                continue
            if j.get("ok"):
                return j.get("result")
            desc = j.get("description", "")
            if r.status_code == 429:
                time.sleep(int((j.get("parameters") or {}).get("retry_after", 5)) + 1)
                continue
            if "message is not modified" in desc:
                return None
            if r.status_code >= 500:
                last = TelegramError(f"{method}: {desc}")
                time.sleep(min(30, 2 ** attempt))
                continue
            raise TelegramError(f"{method}: {desc}")
        raise last or TelegramError(f"{method}: невідома помилка")

    # ---- обгортки ----
    def get_updates(self, offset: int | None, timeout: int = 20) -> list[dict]:
        try:
            r = self._session().post(self.base + "getUpdates", timeout=timeout + 15, data={
                "offset": offset, "timeout": timeout,
                "allowed_updates": json.dumps(["message", "callback_query"])})
            j = r.json()
        except (requests.RequestException, ValueError) as e:
            raise TelegramError(f"getUpdates: {self._clean(e)}") from None
        if not j.get("ok"):
            raise TelegramError(f"getUpdates: {j.get('description')}")
        return j["result"]

    def send_message(self, chat_id, text, reply_markup=None, reply_to=None, html=True, preview=False):
        return self.call("sendMessage", chat_id=chat_id, text=text[:4096],
                         parse_mode="HTML" if html else None, reply_markup=reply_markup,
                         reply_to_message_id=reply_to, allow_sending_without_reply=True,
                         disable_web_page_preview=not preview)

    def send_video(self, chat_id, path, caption, reply_markup=None, width=None, height=None, duration=None):
        return self.call("sendVideo", files={"video": path}, http_timeout=600, chat_id=chat_id,
                         caption=caption, parse_mode="HTML", reply_markup=reply_markup,
                         supports_streaming="true", width=width, height=height, duration=duration)

    def edit_caption(self, chat_id, message_id, caption, reply_markup=None):
        return self.call("editMessageCaption", chat_id=chat_id, message_id=message_id,
                         caption=caption, parse_mode="HTML",
                         reply_markup=reply_markup or {"inline_keyboard": []})

    def edit_text(self, chat_id, message_id, text, reply_markup=None):
        return self.call("editMessageText", chat_id=chat_id, message_id=message_id, text=text[:4096],
                         parse_mode="HTML", disable_web_page_preview=True,
                         reply_markup=reply_markup or {"inline_keyboard": []})

    def answer_callback(self, callback_id, text="", alert=False):
        try:
            return self.call("answerCallbackQuery", callback_query_id=callback_id, text=text[:200],
                             show_alert="true" if alert else None)
        except TelegramError as e:  # запит застарів — не критично
            log.debug("answerCallbackQuery: %s", e)
