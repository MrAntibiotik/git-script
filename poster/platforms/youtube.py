"""YouTube Shorts через YouTube Data API v3 (OAuth)."""
from __future__ import annotations

import logging
import re

from .base import Platform, PublishError, TokenExpired

log = logging.getLogger("youtube")

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
]


def _clean(text: str) -> str:
    return re.sub(r"[<>]", "", text or "")


class YouTube(Platform):
    key = "youtube"
    name = "YouTube"

    @property
    def token_file(self):
        return self.tokens_dir / "youtube_token.json"

    # ---------- авторизація ----------
    def authorize(self) -> None:
        from google_auth_oauthlib.flow import InstalledAppFlow

        secrets = self.pcfg["client_secrets_file"]
        flow = InstalledAppFlow.from_client_secrets_file(secrets, SCOPES)
        creds = flow.run_local_server(port=0, access_type="offline", prompt="consent",
                                      authorization_prompt_message="Відкрийте в браузері: {url}",
                                      success_message="Готово! Можна закрити вкладку.")
        self.tokens_dir.mkdir(parents=True, exist_ok=True)
        self.token_file.write_text(creds.to_json(), encoding="utf-8")
        print(f"Токен YouTube збережено: {self.token_file}")

    def _creds(self):
        from google.auth.exceptions import RefreshError
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials

        if not self.token_file.exists():
            raise TokenExpired("немає токена YouTube")
        creds = Credentials.from_authorized_user_file(str(self.token_file), SCOPES)
        if not creds.valid:
            if not creds.refresh_token:
                raise TokenExpired("токен YouTube недійсний")
            try:
                creds.refresh(Request())
            except RefreshError as e:
                raise TokenExpired(f"не вдалося оновити токен YouTube: {e}") from e
            self.token_file.write_text(creds.to_json(), encoding="utf-8")
        return creds

    def _api(self):
        from googleapiclient.discovery import build

        return build("youtube", "v3", credentials=self._creds(), cache_discovery=False)

    @staticmethod
    def _http_error(e) -> Exception:
        status = getattr(getattr(e, "resp", None), "status", 0)
        text = str(e)
        if status == 401:
            return TokenExpired(f"YouTube 401: {text[:200]}")
        if "quotaExceeded" in text:
            return PublishError("Вичерпано денну квоту YouTube API (скидається опівночі за тихоокеанським часом)")
        if "uploadLimitExceeded" in text:
            return PublishError("YouTube: перевищено ліміт завантажень для каналу")
        return PublishError(f"YouTube HTTP {status}: {text[:300]}")

    # ---------- публікація ----------
    def publish(self, video_path: str, meta) -> dict:
        from googleapiclient.errors import HttpError
        from googleapiclient.http import MediaFileUpload

        title = _clean(meta.yt_title)[:100].strip() or "Shorts"
        desc = _clean(meta.yt_description)
        tags_line = " ".join(t for t in meta.hashtags if t.lower() not in desc.lower())
        if tags_line:
            desc = f"{desc}\n\n{tags_line}".strip()
        if self.pcfg.get("add_shorts_hashtag") and "#shorts" not in (desc + title).lower():
            desc = f"{desc}\n#Shorts".strip()
        tags, total = [], 0
        for t in meta.yt_tags:
            t = _clean(t).lstrip("#")[:100]
            if t and total + len(t) + 3 <= 480:
                tags.append(t)
                total += len(t) + 3
        lang = self.pcfg.get("language") or "uk"
        privacy = self.pcfg.get("privacy", "public")
        body = {
            "snippet": {"title": title, "description": desc[:5000], "tags": tags,
                        "categoryId": str(self.pcfg.get("category_id", "20")),
                        "defaultLanguage": lang, "defaultAudioLanguage": lang},
            "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False, "embeddable": True},
        }
        try:
            yt = self._api()
            media = MediaFileUpload(video_path, mimetype="video/mp4", chunksize=8 * 1024 * 1024, resumable=True)
            req = yt.videos().insert(part="snippet,status", body=body, media_body=media,
                                     notifySubscribers=bool(self.pcfg.get("notify_subscribers", True)))
            resp = None
            while resp is None:
                status, resp = req.next_chunk(num_retries=3)
                if status:
                    log.debug("YouTube upload %d%%", int(status.progress() * 100))
        except HttpError as e:
            raise self._http_error(e) from e
        vid = resp["id"]
        out = {"id": vid, "url": f"https://youtube.com/shorts/{vid}"}
        real = (resp.get("status") or {}).get("privacyStatus")
        if real and real != privacy:
            out["note"] = (f"YouTube встановив статус «{real}» замість «{privacy}» — "
                           "ймовірно, API-проєкт ще не пройшов аудит (див. інструкцію)")
        return out

    def get_views(self, result: dict) -> int | None:
        from googleapiclient.errors import HttpError

        try:
            r = self._api().videos().list(part="statistics", id=result["id"]).execute()
        except HttpError as e:
            raise self._http_error(e) from e
        items = r.get("items") or []
        return int(items[0]["statistics"].get("viewCount", 0)) if items else None

    def check(self) -> str:
        from googleapiclient.errors import HttpError

        try:
            r = self._api().channels().list(part="snippet", mine=True).execute()
        except HttpError as e:
            raise self._http_error(e) from e
        items = r.get("items") or []
        return f"канал «{items[0]['snippet']['title']}»" if items else "канал не знайдено"
