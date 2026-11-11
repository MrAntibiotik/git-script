"""Instagram Reels і Facebook Reels через офіційний Graph API (завантаження файлу з ПК)."""
from __future__ import annotations

import logging
import os
import time

import requests

from .base import Platform, PublishError, TokenExpired

log = logging.getLogger("meta")

TOKEN_ERROR_CODES = {102, 190, 463, 467}


class Graph:
    def __init__(self, version: str, token: str):
        if not token:
            raise TokenExpired("не задано META_PAGE_TOKEN")
        self.version = version
        self.token = token
        self.base = f"https://graph.facebook.com/{version}"

    def _check(self, r: requests.Response) -> dict:
        try:
            j = r.json()
        except ValueError:
            raise PublishError(f"Graph API HTTP {r.status_code}: {r.text[:200]}") from None
        err = j.get("error") if isinstance(j, dict) else None
        if err:
            code = err.get("code")
            msg = err.get("error_user_msg") or err.get("message") or str(err)
            if code in TOKEN_ERROR_CODES:
                raise TokenExpired(f"Meta: {msg}")
            raise PublishError(f"Meta ({code}/{err.get('error_subcode')}): {msg}")
        if r.status_code >= 400:
            raise PublishError(f"Graph API HTTP {r.status_code}: {r.text[:200]}")
        return j

    def get(self, path: str, **params) -> dict:
        params["access_token"] = self.token
        try:
            r = requests.get(f"{self.base}/{path.lstrip('/')}", params=params, timeout=60)
        except requests.RequestException as e:
            raise PublishError(f"мережа: {e}") from e
        return self._check(r)

    def post(self, path: str, **data) -> dict:
        data["access_token"] = self.token
        try:
            r = requests.post(f"{self.base}/{path.lstrip('/')}", data=data, timeout=120)
        except requests.RequestException as e:
            raise PublishError(f"мережа: {e}") from e
        return self._check(r)

    def upload_binary(self, url: str, path: str) -> dict:
        size = os.path.getsize(path)
        headers = {"Authorization": f"OAuth {self.token}", "offset": "0", "file_size": str(size)}
        try:
            with open(path, "rb") as f:
                r = requests.post(url, headers=headers, data=f, timeout=1800)
        except requests.RequestException as e:
            raise PublishError(f"завантаження файлу: {e}") from e
        return self._check(r)


class _MetaBase(Platform):
    reauth_cmd = "python poster.py auth meta"

    @property
    def reauth_hint(self) -> str:
        return self.reauth_cmd

    def graph(self, token: str | None = None) -> Graph:
        return Graph(self.cfg["graph_api_version"], token or self.secrets["META_PAGE_TOKEN"])


class Instagram(_MetaBase):
    key = "instagram"
    name = "Instagram"

    def _g(self) -> Graph:
        return self.graph(self.secrets.get("META_IG_TOKEN") or None)

    @property
    def ig_id(self) -> str:
        ig = self.secrets["IG_USER_ID"]
        if not ig:
            raise PublishError("не задано IG_USER_ID")
        return ig

    def publish(self, video_path: str, meta) -> dict:
        g = self._g()
        c = g.post(f"{self.ig_id}/media", media_type="REELS", upload_type="resumable",
                   caption=meta.instagram_text[:2200],
                   share_to_feed="true" if self.pcfg.get("share_to_feed", True) else "false")
        cid = c["id"]
        uri = c.get("uri") or f"https://rupload.facebook.com/ig-api-upload/{g.version}/{cid}"
        g.upload_binary(uri, video_path)

        deadline = time.time() + 20 * 60
        while True:
            st = g.get(cid, fields="status_code,status")
            code = st.get("status_code")
            if code == "FINISHED":
                break
            if code in ("ERROR", "EXPIRED"):
                raise PublishError(f"Instagram відхилив відео: {st.get('status')}")
            if time.time() > deadline:
                raise PublishError("Instagram: обробка відео триває понад 20 хв")
            time.sleep(10)

        mid = g.post(f"{self.ig_id}/media_publish", creation_id=cid)["id"]
        out = {"id": mid, "url": ""}
        try:  # після публікації — помилки не критичні (щоб не опублікувати двічі)
            out["url"] = g.get(mid, fields="permalink").get("permalink", "")
        except Exception as e:  # noqa: BLE001
            log.warning("Instagram permalink: %s", e)
        return out

    def get_views(self, result: dict) -> int | None:
        g = self._g()
        for metric in ("views", "plays", "reach"):
            try:
                data = g.get(f"{result['id']}/insights", metric=metric).get("data") or []
            except PublishError:
                continue
            if data:
                d = data[0]
                vals = d.get("values") or []
                if vals:
                    return int(vals[0].get("value") or 0)
                if d.get("total_value"):
                    return int(d["total_value"].get("value") or 0)
        return None

    def check(self) -> str:
        r = self._g().get(self.ig_id, fields="username")
        return f"@{r.get('username')}"


class Facebook(_MetaBase):
    key = "facebook"
    name = "Facebook"

    @property
    def page_id(self) -> str:
        pid = self.secrets["FB_PAGE_ID"]
        if not pid:
            raise PublishError("не задано FB_PAGE_ID")
        return pid

    def publish(self, video_path: str, meta) -> dict:
        g = self.graph()
        start = g.post(f"{self.page_id}/video_reels", upload_phase="start")
        vid = start["video_id"]
        url = start.get("upload_url") or f"https://rupload.facebook.com/video-upload/{g.version}/{vid}"
        g.upload_binary(url, video_path)
        g.post(f"{self.page_id}/video_reels", upload_phase="finish", video_id=vid,
               video_state="PUBLISHED", description=meta.facebook_text[:2200])
        out = {"id": vid, "url": f"https://www.facebook.com/reel/{vid}"}

        deadline = time.time() + 15 * 60
        while time.time() < deadline:
            try:
                st = g.get(vid, fields="status").get("status") or {}
            except PublishError as e:
                log.warning("Facebook status: %s", e)
                break
            pub = (st.get("publishing_phase") or {}).get("status")
            proc = (st.get("processing_phase") or {}).get("status")
            if st.get("video_status") == "error" or "error" in (pub, proc):
                errs = (st.get("processing_phase") or {}).get("errors") or st
                raise PublishError(f"Facebook відхилив відео: {errs}")
            if pub == "complete" or st.get("video_status") == "ready":
                return out
            time.sleep(10)
        out["note"] = "Facebook ще обробляє відео — з'явиться на сторінці трохи пізніше"
        return out

    def get_views(self, result: dict) -> int | None:
        data = self.graph().get(f"{result['id']}/video_insights").get("data") or []
        by_name = {d.get("name"): d for d in data}
        for name in ("fb_reels_total_plays", "blue_reels_play_count", "post_video_views", "total_video_views"):
            d = by_name.get(name)
            if d and d.get("values"):
                return int(d["values"][0].get("value") or 0)
        return None

    def check(self) -> str:
        r = self.graph().get(self.page_id, fields="name")
        return f"сторінка «{r.get('name')}»"


def authorize_meta(cfg: dict) -> None:
    """Обмін короткого user-токена на «вічний» токен сторінки; запис у .env."""
    from dotenv import set_key

    s = cfg["secrets"]
    ver = cfg["graph_api_version"]
    app_id, app_secret = s["META_APP_ID"], s["META_APP_SECRET"]
    if not app_id or not app_secret:
        print("Спершу заповніть META_APP_ID і META_APP_SECRET у .env")
        return
    short = input("Вставте короткий User Access Token з Graph API Explorer: ").strip()
    r = requests.get(f"https://graph.facebook.com/{ver}/oauth/access_token", timeout=60, params={
        "grant_type": "fb_exchange_token", "client_id": app_id,
        "client_secret": app_secret, "fb_exchange_token": short}).json()
    if "access_token" not in r:
        print("Помилка обміну токена:", r.get("error", r))
        return
    long_user = r["access_token"]
    pages = requests.get(f"https://graph.facebook.com/{ver}/me/accounts", timeout=60, params={
        "access_token": long_user,
        "fields": "id,name,access_token,instagram_business_account{id,username}"}).json()
    data = pages.get("data") or []
    if not data:
        print("Не знайдено жодної сторінки. Перевірте дозволи pages_show_list / pages_manage_posts.", pages)
        return
    for i, p in enumerate(data, 1):
        ig = p.get("instagram_business_account") or {}
        print(f"{i}. {p['name']} (id {p['id']}) — Instagram: @{ig.get('username', '—')}")
    idx = 0
    if len(data) > 1:
        idx = int(input("Номер сторінки: ").strip() or "1") - 1
    page = data[idx]
    env = cfg["env_path"]
    if not os.path.exists(env):
        open(env, "a", encoding="utf-8").close()
    set_key(env, "META_PAGE_TOKEN", page["access_token"])
    set_key(env, "FB_PAGE_ID", page["id"])
    ig = (page.get("instagram_business_account") or {}).get("id")
    if ig:
        set_key(env, "IG_USER_ID", ig)
    else:
        print("⚠️ До сторінки не прив'язано Instagram (професійний акаунт) — IG_USER_ID не записано.")
    print(f"Готово: токен сторінки «{page['name']}» записано в {env}")
