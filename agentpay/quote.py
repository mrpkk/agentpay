"""Котировки: подписанное обязательство «столько-то за этот вызов».

Котировка — единственный носитель цены между продавцом и рельсом. Рельс
не имеет права её изменить (Часть VIII.7, правило 4), поэтому целостность
котировки проверяется подписью по канонической сериализации.
"""

from __future__ import annotations

import hmac
import json
import secrets
import time
from dataclasses import dataclass
from decimal import Decimal
from hashlib import sha256

__all__ = ["SCHEME_EXACT", "SCHEME_UPTO", "Quote", "sign_quote"]

SCHEME_EXACT = "exact"
"""Клиент платит ровно указанную сумму за один вызов."""

SCHEME_UPTO = "upto"
"""Клиент авторизует потолок; фактическое списание не выше (pay-per-inference)."""

DEFAULT_TTL_SECONDS = 300


@dataclass(frozen=True)
class Quote:
    """Подписанное предложение с привязкой к конкретному ресурсу.

    Привязка (`resource`) — то, что делает котировку одноразовой по смыслу:
    оплата авторизует ровно этот вызов этого ресурса, а не «что угодно
    у продавца». Именно это отличает подлинную авторизацию от поддельной
    (аналог Verifiable Intent, но для артефактов, а не для карт).
    """

    quote_id: str
    item: str
    amount: Decimal
    currency: str
    rail: str
    scheme: str
    resource: str
    nonce: str
    issued_at: int
    expires_at: int
    max_amount: Decimal | None = None
    signature: str = ""

    def __post_init__(self) -> None:
        if self.scheme not in (SCHEME_EXACT, SCHEME_UPTO):
            raise ValueError(f"неизвестная схема: {self.scheme!r}")
        if self.amount < 0:
            raise ValueError(f"отрицательная сумма: {self.amount}")
        if self.expires_at <= self.issued_at:
            raise ValueError("срок действия должен быть положительным")
        if not self.resource.strip():
            raise ValueError("resource обязателен: без него оплата не адресована")
        if self.scheme == SCHEME_UPTO:
            if self.max_amount is None:
                raise ValueError("схема upto требует max_amount (потолка)")
            if self.max_amount < self.amount:
                raise ValueError("max_amount меньше amount")

    @property
    def ceiling(self) -> Decimal:
        """Потолок авторизации: для upto — max_amount, иначе сама сумма."""
        if self.scheme == SCHEME_UPTO and self.max_amount is not None:
            return self.max_amount
        return self.amount

    def is_expired(self, now: int | None = None) -> bool:
        current = int(time.time()) if now is None else now
        return current >= self.expires_at

    def _payload(self) -> str:
        """Каноническая сериализация — стабильный порядок и формат полей."""
        document = {
            "quote_id": self.quote_id,
            "item": self.item,
            "amount": str(self.amount),
            "currency": self.currency,
            "rail": self.rail,
            "scheme": self.scheme,
            "resource": self.resource,
            "nonce": self.nonce,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
        }
        if self.max_amount is not None:
            document["max_amount"] = str(self.max_amount)
        return json.dumps(document, sort_keys=True, separators=(",", ":"))

    def digest(self) -> str:
        return sha256(self._payload().encode("utf-8")).hexdigest()

    def with_signature(self, secret: bytes) -> "Quote":
        return _replace(self, signature=hmac.new(
            secret, self._payload().encode("utf-8"), sha256
        ).hexdigest())

    def signature_valid(self, secret: bytes) -> bool:
        if not self.signature:
            return False
        expected = hmac.new(
            secret, self._payload().encode("utf-8"), sha256
        ).hexdigest()
        return hmac.compare_digest(expected, self.signature)


def _replace(quote: Quote, **changes: object) -> Quote:
    from dataclasses import replace

    return replace(quote, **changes)  # type: ignore[arg-type]


def new_nonce() -> str:
    """Криптографический одноразовый идентификатор авторизации."""
    return secrets.token_urlsafe(24)


def sign_quote(
    quote_id: str,
    item: str,
    amount: Decimal,
    currency: str,
    rail: str,
    resource: str,
    secret: bytes,
    *,
    scheme: str = SCHEME_EXACT,
    max_amount: Decimal | None = None,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    now: int | None = None,
) -> Quote:
    """Создать подписанную котировку с одноразовым nonce и сроком действия."""
    issued = int(time.time()) if now is None else now
    if ttl_seconds <= 0:
        raise ValueError("ttl_seconds должен быть положительным")
    unsigned = Quote(
        quote_id=quote_id,
        item=item,
        amount=amount,
        currency=currency,
        rail=rail,
        scheme=scheme,
        resource=resource,
        nonce=new_nonce(),
        issued_at=issued,
        expires_at=issued + ttl_seconds,
        max_amount=max_amount,
    )
    return unsigned.with_signature(secret)
