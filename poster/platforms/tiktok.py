"""TikTok через Content Posting API (Direct Post, FILE_UPLOAD)."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets as pysecrets
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

import requests

from .base import Platform, PublishError, TokenExpired

log = logging.getLogger("tiktok")

API = "https://open.tiktokapis.com"
AUTH_URL = "https://www.tiktok.com/v2/auth/authorize/"
SCOPES = "user.info.basic,video.publish,video.list"
MB = 1024 * 1024
TOKEN_ERRORS = {"access_token_invalid", "token_not_authorized_for_specified_scope",
                "scope_not_authorized", "invalid_token"}


class TikTok(Platform):
    key = "tiktok"
    name = "TikTok"

    @property
    def token_file(self):
        return self.tokens_dir / "tiktok_token.json"

    # ---------- токени ----------
    def _save(self, j: dict) -> dict:
        now = int(time.time())
        tok = {
            "access_token": j["access_token"],
            "refresh_token": j.get("refresh_token"),
            "open_id": j.get("open_id"),
            "scope": j.get("scope"),
            "expires_at": now + int(j.get("expires_in", 86400)),
            "refresh_expires_at": now + int(j.get("refresh_expires_in", 365 * 86400)),
        }
        self.tokens_dir.mkdir(parents=True, exist_ok=True)
        self.token_file.write_text(json.dumps(tok, indent=2), encoding="utf-8")
        return tok

    def _oauth(self, **data) -> dict:
        data.update(client_key=self.secrets["TIKTOK_CLIENT_KEY"],
                    client_secret=self.secrets["TIKTOK_CLIENT_SECRET"])
        try:
            r = requests.post(f"{API}/v2/oauth/token/", data=data, timeout=60,
                              headers={"Content-Type": "application/x-www-form-urlencoded"})
            return r.json()
        except (requests.RequestException, ValueError) as e:
            raise PublishError(f"TikTok OAuth: {e}") from e

    def _token(self) -> str:
        if not self.token_file.exists():
            raise TokenExpired("немає токена TikTok")
        tok = json.loads(self.token_file.read_text(encoding="utf-8"))
        if tok.get("expires_at", 0) - 300 > time.time():
            return tok["access_token"]
        if not tok.get("refresh_token") or tok.get("refresh_expires_at", 0) < time.time():
            raise TokenExpired("refresh-токен TikTok протерміновано")
        j = self._oauth(grant_type="refresh_token", refresh_token=tok["refresh_token"])
        if "access_token" not in j:
            raise TokenExpired(f"не вдалося оновити токен TikTok: {j.get('error_description') or j}")
        return self._save(j)["access_token"]

    def _post(self, path: str, body: dict, params: dict | None = None) -> dict:
        headers = {"Authorization": f"Bearer {self._token()}",
                   "Content-Type": "application/json; charset=UTF-8"}
        try:
            r = requests.post(f"{API}{path}", headers=headers, json=body, params=params, timeout=60)
            j = r.json()
        except (requests.RequestException, ValueError) as e:
            raise PublishError(f"TikTok {path}: {e}") from e
        err = j.get("error") or {}
        code = err.get("code")
        if code and code != "ok":
            msg = f"TikTok {code}: {err.get('message')}"
            if code in TOKEN_ERRORS or r.status_code == 401:
                raise TokenExpired(msg)
            raise PublishError(msg)
        return j.get("data") or {}

    # ---------- авторизація (Login Kit, PKCE) ----------
    def authorize(self) -> None:
        if not self.secrets["TIKTOK_CLIENT_KEY"] or not self.secrets["TIKTOK_CLIENT_SECRET"]:
            print("Спершу заповніть TIKTOK_CLIENT_KEY і TIKTOK_CLIENT_SECRET у .env")
            return
        redirect = self.pcfg["redirect_uri"]
        verifier = pysecrets.token_urlsafe(64)[:64]
        challenge = hashlib.sha256(verifier.encode()).hexdigest()  # TikTok (desktop) очікує hex
        state = pysecrets.token_hex(8)
        url = AUTH_URL + "?" + urlencode({
            "client_key": self.secrets["TIKTOK_CLIENT_KEY"], "response_type": "code", "scope": SCOPES,
            "redirect_uri": redirect, "state": state,
            "code_challenge": challenge, "code_challenge_method": "S256"})
        u = urlparse(redirect)
        code = None
        print("Відкрийте посилання та дозвольте доступ:\n", url)
        if u.hostname in ("localhost", "127.0.0.1"):
            got: dict = {}

            class H(BaseHTTPRequestHandler):
                def do_GET(self):  # noqa: N802
                    q = parse_qs(urlparse(self.path).query)
                    got.update({k: v[0] for k, v in q.items()})
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.end_headers()
                    self.wfile.write("Готово! Можна закрити вкладку.".encode())

                def log_message(self, *a):
                    pass

            srv = HTTPServer((u.hostname, u.port or 80), H)
            threading.Thread(target=lambda: webbrowser.open(url), daemon=True).start()
            while "code" not in got and "error" not in got:
                srv.handle_request()
            srv.server_close()
            if got.get("state") != state or "code" not in got:
                print("Помилка авторизації:", got)
                return
            code = got["code"]
        else:
            webbrowser.open(url)
            back = input("Вставте повну адресу, на яку вас перенаправило: ").strip()
            q = parse_qs(urlparse(back).query)
            code = (q.get("code") or [""])[0]
        j = self._oauth(grant_type="authorization_code", code=code, redirect_uri=redirect,
                        code_verifier=verifier)
        if "access_token" not in j:
            print("Помилка отримання токена:", j)
            return
        tok = self._save(j)
        print(f"Токен TikTok збережено: {self.token_file} (scope: {tok.get('scope')})")

    # ---------- публікація ----------
    def _creator(self) -> dict:
        return self._post("/v2/post/publish/creator_info/query/", {})

    def publish(self, video_path: str, meta) -> dict:
        creator = self._creator()
        username = creator.get("creator_username") or ""
        options = creator.get("privacy_level_options") or []
        want = "PUBLIC_TO_EVERYONE" if self.pcfg.get("public") else "SELF_ONLY"
        note = ""
        if options and want not in options:
            note = f"TikTok не дозволяє {want}, опубліковано як SELF_ONLY"
            want = "SELF_ONLY"

        size = os.path.getsize(video_path)
        if size <= 64 * MB:
            chunk, count = size, 1
        else:
            chunk = 10 * MB
            count = size // chunk  # останній шматок забирає залишок (≤ 20 МБ)
        init = self._post("/v2/post/publish/video/init/", {
            "post_info": {
                "title": meta.tiktok_text[:2200],
                "privacy_level": want,
                "disable_comment": bool(self.pcfg.get("disable_comment")),
                "disable_duet": bool(self.pcfg.get("disable_duet")),
                "disable_stitch": bool(self.pcfg.get("disable_stitch")),
                "video_cover_timestamp_ms": 1000,
            },
            "source_info": {"source": "FILE_UPLOAD", "video_size": size,
                            "chunk_size": chunk, "total_chunk_count": count},
        })
        publish_id, upload_url = init["publish_id"], init["upload_url"]

        with open(video_path, "rb") as f:
            for i in range(count):
                start = i * chunk
                end = size - 1 if i == count - 1 else start + chunk - 1
                f.seek(start)
                data = f.read(end - start + 1)
                try:
                    r = requests.put(upload_url, data=data, timeout=900, headers={
                        "Content-Type": "video/mp4", "Content-Length": str(len(data)),
                        "Content-Range": f"bytes {start}-{end}/{size}"})
                except requests.RequestException as e:
                    raise PublishError(f"TikTok upload: {e}") from e
                if r.status_code not in (200, 201, 206):
                    raise PublishError(f"TikTok upload HTTP {r.status_code}: {r.text[:200]}")

        out = {"id": publish_id, "url": f"https://www.tiktok.com/@{username}" if username else "",
               "privacy": want}
        deadline = time.time() + 15 * 60
        while time.time() < deadline:
            time.sleep(10)
            try:
                st = self._post("/v2/post/publish/status/fetch/", {"publish_id": publish_id})
            except PublishError as e:
                log.warning("TikTok status: %s", e)
                continue
            status = st.get("status")
            if status == "FAILED":
                raise PublishError(f"TikTok: {st.get('fail_reason')}")
            if status == "PUBLISH_COMPLETE":
                ids = st.get("publicaly_available_post_id") or st.get("publicly_available_post_id") or []
                if ids:
                    out["post_id"] = str(ids[0])
                    out["url"] = f"https://www.tiktok.com/@{username}/video/{ids[0]}"
                break
        else:
            note = (note + "; " if note else "") + "TikTok ще обробляє відео"
        if want == "SELF_ONLY":
            note = (note + "; " if note else "") + "опубліковано приватно — відкрийте вручну в застосунку"
        if note:
            out["note"] = note
        return out

    def get_views(self, result: dict) -> int | None:
        pid = result.get("post_id")
        if not pid:
            return None  # приватні пости не мають публічного id
        data = self._post("/v2/video/query/", {"filters": {"video_ids": [pid]}},
                          params={"fields": "id,view_count"})
        vids = data.get("videos") or []
        return int(vids[0].get("view_count", 0)) if vids else None

    def check(self) -> str:
        c = self._creator()
        return (f"@{c.get('creator_username')}, доступні рівні приватності: "
                f"{', '.join(c.get('privacy_level_options') or [])}")
