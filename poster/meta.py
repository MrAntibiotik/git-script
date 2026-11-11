"""Читання метаданих шортса (json від generate_meta.py) і робота з файлами."""
from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

HASHTAG_RE = re.compile(r"#[\w\d_]+", re.UNICODE)


@dataclass
class PostMeta:
    yt_title: str
    yt_description: str = ""
    yt_tags: list[str] = field(default_factory=list)
    hashtags: list[str] = field(default_factory=list)
    tiktok_text: str = ""
    instagram_text: str = ""
    facebook_text: str = ""
    game: str = ""


def read_json(path: str | Path) -> dict:
    with open(path, encoding="utf-8-sig") as f:
        data = json.load(f)
    return data if isinstance(data, dict) else {}


def write_json(path: str | Path, data: dict) -> None:
    tmp = Path(str(path) + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _pick(d, *keys) -> str:
    if isinstance(d, str):
        return d.strip()
    if not isinstance(d, dict):
        return ""
    for k in keys:
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
        if isinstance(v, list) and v and all(isinstance(x, str) for x in v):
            return " ".join(v).strip()
    return ""


def _as_list(v) -> list[str]:
    if isinstance(v, str):
        v = re.split(r"[,\n]", v) if "," in v or "\n" in v else v.split()
    if not isinstance(v, list):
        return []
    return [str(x).strip() for x in v if str(x).strip()]


def _hashtags(v) -> list[str]:
    out = []
    for t in _as_list(v):
        t = t.strip()
        if not t:
            continue
        t = "#" + t.lstrip("#").replace(" ", "")
        if t not in out:
            out.append(t)
    return out


def _with_hashtags(text: str, tags: list[str]) -> str:
    """Додає хештеги в кінець тексту, якщо в тексті їх ще немає."""
    if not tags or HASHTAG_RE.search(text or ""):
        return text
    return (text + "\n\n" + " ".join(tags)).strip()


def json_status(data: dict) -> str:
    st = data.get("status")
    if isinstance(st, dict):
        st = st.get("state") or st.get("status")
    return str(st or "")


def load_meta(json_path: str | Path, title_override: str | None = None) -> PostMeta:
    """Гнучко читає формат generate_meta.py.

    Очікувано: {"youtube": {"title", "description", "tags", "hashtags"},
                "tiktok": {"caption"} | "текст", "instagram": {"caption"} | "текст",
                "facebook": (необов'язково, інакше береться instagram)}
    """
    data = read_json(json_path)
    yt = data.get("youtube") or data.get("yt") or {}
    tt = data.get("tiktok") or data.get("tt") or {}
    ig = data.get("instagram") or data.get("ig") or data.get("reels") or {}
    fb = data.get("facebook") or data.get("fb") or {}

    title = _pick(yt, "title", "name") or _pick(data, "title") or Path(json_path).stem
    desc = _pick(yt, "description", "desc", "text")
    tags = _as_list(yt.get("tags") if isinstance(yt, dict) else None) or _as_list(data.get("tags"))
    hashtags = _hashtags((yt.get("hashtags") if isinstance(yt, dict) else None) or data.get("hashtags"))
    if not hashtags:
        hashtags = _hashtags(HASHTAG_RE.findall(f"{title} {desc}"))

    tt_text = _pick(tt, "caption", "text", "description", "title")
    tt_tags = _hashtags(tt.get("hashtags")) if isinstance(tt, dict) else []
    ig_text = _pick(ig, "caption", "text", "description")
    ig_tags = _hashtags(ig.get("hashtags")) if isinstance(ig, dict) else []
    fb_text = _pick(fb, "caption", "text", "description")
    fb_tags = _hashtags(fb.get("hashtags")) if isinstance(fb, dict) else []

    tt_text = _with_hashtags(tt_text or title, tt_tags or hashtags)
    ig_text = _with_hashtags(ig_text or title, ig_tags or hashtags)
    fb_text = _with_hashtags(fb_text, fb_tags) if fb_text else ig_text

    return PostMeta(
        yt_title=(title_override or title).strip(),
        yt_description=desc,
        yt_tags=tags,
        hashtags=hashtags,
        tiktok_text=tt_text,
        instagram_text=ig_text,
        facebook_text=fb_text,
        game=str(data.get("game") or Path(json_path).parent.name),
    )


def move_pair(mp4: str, json_path: str, dest_dir: Path) -> tuple[str, str]:
    """Переносить mp4 + json у dest_dir, уникаючи конфліктів імен."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    src_mp4, src_json = Path(mp4), Path(json_path)
    stem = src_mp4.stem
    n = 1
    while (dest_dir / f"{stem}.mp4").exists() or (dest_dir / f"{stem}.json").exists():
        n += 1
        stem = f"{src_mp4.stem}_{n}"
    new_mp4, new_json = dest_dir / f"{stem}.mp4", dest_dir / f"{stem}.json"
    if src_mp4.exists():
        shutil.move(str(src_mp4), str(new_mp4))
    if src_json.exists():
        shutil.move(str(src_json), str(new_json))
    return str(new_mp4), str(new_json)
