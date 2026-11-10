"""Зрозумілі користувачу помилки (українською, з порадою, що зробити)."""


class UserError(Exception):
    """Помилка, яку бачить користувач: що сталося + що зробити."""

    def __init__(self, message: str, hint: str | None = None):
        super().__init__(message)
        self.message = message
        self.hint = hint

    def __str__(self) -> str:
        if self.hint:
            return f"{self.message}\n   Що зробити: {self.hint}"
        return self.message
