"""Спільний інтерфейс для модулів платформ."""
from __future__ import annotations

from pathlib import Path


class TokenExpired(Exception):
    """Токен протерміновано/відкликано — потрібна повторна авторизація (без повторних спроб)."""


class PublishError(Exception):
    """Будь-яка інша помилка публікації (буде повторена згідно з retry)."""


class Platform:
    key = ""
    name = ""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.pcfg = cfg["platforms"].get(self.key, {})
        self.tokens_dir = Path(cfg["paths"]["tokens"])
        self.secrets = cfg["secrets"]

    def publish(self, video_path: str, meta) -> dict:
        """Публікує ОРИГІНАЛЬНИЙ файл. Повертає {'id', 'url', 'note'?, ...}."""
        raise NotImplementedError

    def get_views(self, result: dict) -> int | None:
        raise NotImplementedError

    def check(self) -> str:
        """Перевірка доступу; повертає короткий опис акаунта або кидає виняток."""
        raise NotImplementedError

    @property
    def reauth_hint(self) -> str:
        return f"python poster.py auth {self.key}"
