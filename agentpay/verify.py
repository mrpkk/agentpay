"""Верификация авторизации: вердикт всегда из конечного набора.

Ядро проверки не знает ни про один платёжный рельс — оно проверяет
подпись котировки, срок, одноразовость nonce и соответствие суммы
каталогу. Рельс отвечает только за то, чем он владеет (сеть, подпись
оператора, расчёт), и подставляется снаружи как адаптер.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from threading import Lock

from .pricing import PriceList
from .quote import SCHEME_UPTO, Quote

__all__ = ["Verdict", "VerdictReason", "NonceStore", "MemoryNonceStore", "check"]


class Verdict(str, Enum):
    """Конечный набор вердиктов. Расширять запрещено (Часть VIII.7, п.5)."""

    ACCEPT = "accept"
    REVIEW = "review"
    REJECT = "reject"


class VerdictReason(str, Enum):
    """Машиночитаемая причина — обязательна, чтобы решение можно было аудитить."""

    OK = "ok"
    QUOTE_NOT_SIGNED = "quote_not_signed"
    SIGNATURE_INVALID = "signature_invalid"
    QUOTE_EXPIRED = "quote_expired"
    REPLAY_DETECTED = "replay_detected"
    AMOUNT_MISMATCH = "amount_mismatch"
    CEILING_EXCEEDED = "ceiling_exceeded"
    UNKNOWN_ITEM = "unknown_item"
    CURRENCY_MISMATCH = "currency_mismatch"
    RAIL_MISMATCH = "rail_mismatch"
    RESOURCE_UNBOUND = "resource_unbound"


class NonceStore:
    """Контракт хранилища одноразовости. Реализация может быть файловой,
    сетевой или в памяти; интерфейс обязателен, чтобы replay-защита не
    была опциональной.

    `claim` — операция, ради которой контракт и выделен отдельно: `seen`
    плюс `remember` — это две неатомарные операции, между которыми второй
    процесс успевает увидеть «свободно» и выполнить работу дважды.
    """

    def seen(self, nonce: str) -> bool:  # pragma: no cover - интерфейс
        raise NotImplementedError

    def remember(self, nonce: str, expires_at: int) -> None:  # pragma: no cover
        raise NotImplementedError

    def claim(self, nonce: str, expires_at: int) -> bool:  # pragma: no cover
        """Атомарно занять nonce. True — занял я, False — уже занят."""
        raise NotImplementedError

    def forget(self, nonce: str) -> None:  # pragma: no cover
        """Отпустить занятый nonce (откат при неудачном исполнении)."""
        raise NotImplementedError


@dataclass
class MemoryNonceStore(NonceStore):
    """Одноразовость в памяти процесса, с блокировкой на запись.

    Ограничение осознанное: защита от гонок в пределах процесса, но не между
    процессами — для этого нужен общий файловый или сетевой стор.
    """

    _seen: dict[str, int] = field(default_factory=dict)
    _lock: Lock = field(default_factory=Lock, repr=False, compare=False)

    def seen(self, nonce: str) -> bool:
        with self._lock:
            expires_at = self._seen.get(nonce)
            if expires_at is None:
                return False
            if expires_at <= int(time.time()):
                del self._seen[nonce]
                return False
            return True

    def remember(self, nonce: str, expires_at: int) -> None:
        with self._lock:
            self._seen[nonce] = expires_at

    def claim(self, nonce: str, expires_at: int) -> bool:
        with self._lock:
            existing = self._seen.get(nonce)
            if existing is not None and existing > int(time.time()):
                return False
            self._seen[nonce] = expires_at
            return True

    def forget(self, nonce: str) -> None:
        with self._lock:
            self._seen.pop(nonce, None)


@dataclass(frozen=True)
class _Outcome:
    verdict: Verdict
    reason: VerdictReason
    detail: str = ""


def check(
    quote: Quote,
    pricelist: PriceList,
    secret: bytes,
    *,
    now: int | None = None,
    nonces: NonceStore | None = None,
    expected_resource: str | None = None,
    expect_rail: str | None = None,
) -> tuple[Verdict, VerdictReason, str]:
    """Проверить котировку до выполнения вызова.

    Порядок проверок — от дешёвой и необратимой к дорогой: подпись, срок,
    одноразовость, соответствие каталогу. Первое нарушение определяет вердикт.
    """
    if not quote.signature:
        return Verdict.REJECT, VerdictReason.QUOTE_NOT_SIGNED, ""
    if not quote.signature_valid(secret):
        return Verdict.REJECT, VerdictReason.SIGNATURE_INVALID, ""
    if quote.is_expired(now):
        return Verdict.REJECT, VerdictReason.QUOTE_EXPIRED, ""

    if not quote.resource.strip():
        return Verdict.REJECT, VerdictReason.RESOURCE_UNBOUND, ""
    if expected_resource is not None and quote.resource != expected_resource:
        return Verdict.REJECT, VerdictReason.RESOURCE_UNBOUND, (
            f"котировка адресована {quote.resource!r}, "
            f"ожидался {expected_resource!r}"
        )

    if nonces is not None and nonces.seen(quote.nonce):
        return Verdict.REJECT, VerdictReason.REPLAY_DETECTED, ""

    if expect_rail is not None and quote.rail != expect_rail:
        return Verdict.REJECT, VerdictReason.RAIL_MISMATCH, ""

    try:
        expected_amount = pricelist.price_of(quote.item)
    except KeyError:
        return Verdict.REJECT, VerdictReason.UNKNOWN_ITEM, quote.item

    if pricelist.currency != quote.currency:
        return Verdict.REJECT, VerdictReason.CURRENCY_MISMATCH, (
            f"{quote.currency} != {pricelist.currency}"
        )

    if quote.amount > expected_amount:
        return Verdict.REJECT, VerdictReason.AMOUNT_MISMATCH, (
            f"предложено {quote.amount} при каталоговой {expected_amount}"
        )

    if quote.scheme == SCHEME_UPTO:
        ceiling = pricelist.upto(quote.item)
        if quote.ceiling > ceiling:
            return Verdict.REJECT, VerdictReason.CEILING_EXCEEDED, (
                f"потолок {quote.ceiling} выше каталожного {ceiling}"
            )

    if quote.amount != expected_amount:
        # Не отказ, а повод для ручного смотра: сумма ниже каталожной цены
        # может быть честной скидкой, а может быть попыткой получить
        # бесплатный вызов. Решает человек.
        return Verdict.REVIEW, VerdictReason.AMOUNT_MISMATCH, (
            f"предложено {quote.amount} при каталожной {expected_amount}"
        )

    return Verdict.ACCEPT, VerdictReason.OK, ""


def consume(nonces: NonceStore, quote: Quote) -> None:
    """Пометить nonce использованным — только после успешной проверки."""
    nonces.remember(quote.nonce, quote.expires_at)


def settle_amount(quote: Quote, actual: Decimal) -> Decimal:
    """Фактическое списание по схеме `upto`: не выше авторизованного потолка.

    Для `exact` сумма фиксирована. Клиент, сообщивший `actual` больше
    потолка, получает цену потолка, а не штраф — превышение фиксируется
    в метрике, а не наказывается несправедливо.
    """
    if actual < 0:
        raise ValueError(f"фактическое потребление отрицательно: {actual}")
    if quote.scheme == SCHEME_UPTO:
        return min(actual, quote.ceiling)
    return quote.amount
