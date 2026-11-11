from __future__ import annotations

from config import enabled_platforms

from .base import Platform, PublishError, TokenExpired


def get_class(key: str):
    if key == "youtube":
        from .youtube import YouTube
        return YouTube
    if key == "tiktok":
        from .tiktok import TikTok
        return TikTok
    if key == "instagram":
        from .meta_graph import Instagram
        return Instagram
    if key == "facebook":
        from .meta_graph import Facebook
        return Facebook
    raise KeyError(key)


def build_platforms(cfg: dict) -> dict[str, Platform]:
    return {k: get_class(k)(cfg) for k in enabled_platforms(cfg)}


__all__ = ["Platform", "PublishError", "TokenExpired", "build_platforms", "get_class"]
