"""Rail-адаптеры: единый контракт, разные расчётные сети.

Роволь v4, часть VIII.7: в agentic-коммерции одновременно живут ACP, UCP,
AP2, MPP, x402, MCP, A2A и WebMCP, а карточные сети уже сводят часть из
них в одну интеграцию. Привязка ядра к одному рельсу — техдолг, поэтому
рельс подключается адаптером и не влияет на цену.

Каждый адаптер реализует минимальный контракт:
    verify_authorization(proof) -> Verdict
    build_authorization(quote)  -> Proof
    settle(proof, outcome)      -> Settlement
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from threading import Lock
from typing import Protocol, runtime_checkable

from .pricing import PriceList
from .quote import Quote
from .verify import (
    MemoryNonceStore,
    NonceStore,
    Verdict,
    VerdictReason,
    check,
    settle_amount,
)

__all__ = [
    "Rail",
    "Proof",
    "Settlement",
    "X402Rail",
    "MandateRail",
    "MockRail",
    "register",
    "get",
    "available",
]


@dataclass(frozen=True)
class Proof:
    """Доказательство оплаты, предъявляемое рельсу продавцом."""

    quote: Quote
    payer: str
    transaction_ref: str
    payload: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Settlement:
    """Результат расчёта. Сумма не может превысить авторизованный потолок."""

    settled: Decimal
    currency: str
    transaction_ref: str
    status: str


@runtime_checkable
class Rail(Protocol):
    """Минимальный контракт рельса (Часть VIII.7, п.2).

    `authorize` обязателен, а не опционален: именно он захватывает или
    резервирует средства **до** исполнения работы. Если контур будет
    проверять котировку сам, минуя рельс, лимиты мандата и одноразовость
    останутся мёртвым кодом.
    """

    name: str

    def authorize(
        self,
        proof: Proof,
        *,
        now: int | None = None,
        expected_resource: str | None = None,
    ) -> Verdict: ...
    def verify_authorization(self, proof: Proof) -> Verdict: ...
    def build_authorization(self, quote: Quote) -> Proof: ...
    def settle(self, proof: Proof, outcome: Decimal) -> Settlement: ...
    def release(self, proof: Proof) -> None: ...


class _BaseRail:
    """Общая часть адаптеров: проверка котировки, которую не имеет права
    выполнить ни один рельс, и расчёт по правилам каталога."""

    name = "base"

    def __init__(
        self,
        pricelist: PriceList,
        secret: bytes,
        *,
        nonces: NonceStore | None = None,
    ) -> None:
        self.pricelist = pricelist
        self.secret = secret
        self.nonces = nonces if nonces is not None else MemoryNonceStore()

    def _gate(
        self,
        proof: Proof,
        *,
        now: int | None = None,
        expected_resource: str | None = None,
    ) -> tuple[Verdict, VerdictReason, str]:
        return check(
            proof.quote,
            self.pricelist,
            self.secret,
            now=now,
            nonces=self.nonces,
            expect_rail=self.name,
            expected_resource=expected_resource,
        )

    def build_authorization(self, quote: Quote) -> Proof:
        return Proof(
            quote=quote,
            payer=self.name,
            transaction_ref=f"{self.name}:{quote.quote_id}",
        )

    def authorize(
        self,
        proof: Proof,
        *,
        now: int | None = None,
        expected_resource: str | None = None,
    ) -> Verdict:
        """Проверить и захватить авторизацию до исполнения.

        Claim атомарен: `seen` + `remember` оставили бы окно, в которое
        вторая авторизация успела бы пройти и продублировать работу.
        """
        verdict, _reason, _detail = self._gate(
            proof, now=now, expected_resource=expected_resource
        )
        if verdict is not Verdict.ACCEPT:
            return verdict
        if not self.nonces.claim(proof.quote.nonce, proof.quote.expires_at):
            return Verdict.REJECT
        return Verdict.ACCEPT

    def verify_authorization(self, proof: Proof) -> Verdict:
        """Совместимость с прежним контрактом: без привязки к ресурсу."""
        return self.authorize(proof)

    def release(self, proof: Proof) -> None:
        """Откат захвата: работа не выполнена — авторизация не израсходована."""
        self.nonces.forget(proof.quote.nonce)


class X402Rail(_BaseRail):
    """HTTP-native платежи, устоявшиеся на микроплатежах.

    Источник контекста: x402 под управлением Linux Foundation, участники
    включают Visa, Mastercard, American Express, Shopify, Circle, AWS,
    Google; заявленный объём к сентябрю 2026 — свыше 230 млн транзакций на
    $54 млн, то есть средний чек около $0.23. Отсюда роль адаптера: это
    калитка «попробовать без аккаунта» и микрометринг, а не источник выручки.
    """

    name = "x402"

    def settle(self, proof: Proof, outcome: Decimal) -> Settlement:
        amount = settle_amount(proof.quote, outcome)
        return Settlement(
            settled=amount,
            currency=proof.quote.currency,
            transaction_ref=proof.transaction_ref,
            status="settled",
        )


class MandateRail(_BaseRail):
    """Криптографическая авторизация намерения пользователя (класс AP2 /
    Mastercard Verifiable Intent).

    Смысл: доказательство того, что человек разрешил именно этот вызов,
    а не «агент что-то купил». Проверяет не сумму, а связку
    «намерение → ресурс → потолок».

    Отличие от разового платежа: мандат по замыслу многоразовый, поэтому
    одноразовость nonce к нему не применяется — иначе вторая оплата по
    действующему мандату была бы ошибкой. Вместо неё работают суммарный
    лимит расхода и ручной смотр при повторном предъявлении одной котировки.
    """

    name = "mandate"

    def __init__(
        self,
        pricelist: PriceList,
        secret: bytes,
        *,
        nonces: NonceStore | None = None,
        spending_limit: Decimal | None = None,
        mandate_id: str = "default",
    ) -> None:
        super().__init__(pricelist, secret, nonces=nonces)
        self.spending_limit = spending_limit
        self.mandate_id = mandate_id
        self._spent: dict[str, Decimal] = {}
        self._honored: set[str] = set()
        # Резерв — сумма, авторизованная, но ещё не рассчитанная. Без него
        # лимит проверяется только по факту, и десяток параллельных вызовов
        # проходит проверку на одном и том же остатке.
        self._reserved: dict[str, Decimal] = {}
        self._lock = Lock()

    def spent(self) -> Decimal:
        return self._spent.get(self.mandate_id, Decimal(0))

    def reserved(self) -> Decimal:
        return sum(self._reserved.values(), Decimal(0))

    def authorize(
        self,
        proof: Proof,
        *,
        now: int | None = None,
        expected_resource: str | None = None,
    ) -> Verdict:
        """Занять средства в лимите, а не расходовать авторизацию.

        Мандату одноразовость nonce не свойственна — иначе вторая оплата по
        действующему мандату была бы ошибкой. Его ограничение — суммарный
        лимит, и он обязан учитывать всё выданное, включая ещё не
        рассчитанное: иначе десяток параллельных вызовов проходит одну и ту же
        проверку на полном остатке.
        """
        quote = proof.quote
        verdict, _reason, _detail = check(
            quote,
            self.pricelist,
            self.secret,
            now=now,
            nonces=None,
            expect_rail=self.name,
            expected_resource=expected_resource,
        )
        if verdict is not Verdict.ACCEPT:
            return verdict
        with self._lock:
            if self.spending_limit is not None:
                outstanding = self.reserved() - self._reserved.get(
                    quote.quote_id, Decimal(0)
                )
                if self.spent() + outstanding + quote.amount > self.spending_limit:
                    return Verdict.REJECT
            if quote.quote_id in self._honored:
                return Verdict.REVIEW
            self._reserved[quote.quote_id] = quote.amount
        return Verdict.ACCEPT

    def release(self, proof: Proof) -> None:
        """Вернуть резерв: работа не выполнена — лимит не израсходован."""
        with self._lock:
            self._reserved.pop(proof.quote.quote_id, None)

    def settle(self, proof: Proof, outcome: Decimal) -> Settlement:
        amount = settle_amount(proof.quote, outcome)
        with self._lock:
            self._reserved.pop(proof.quote.quote_id, None)
            if self.spending_limit is not None:
                room = self.spending_limit - self.spent()
                if room <= 0:
                    return Settlement(
                        settled=Decimal(0),
                        currency=proof.quote.currency,
                        transaction_ref=proof.transaction_ref,
                        status="limit_exhausted",
                    )
                amount = min(amount, room)
            self._spent[self.mandate_id] = self.spent() + amount
            self._honored.add(proof.quote.quote_id)
            return Settlement(
                settled=amount,
                currency=proof.quote.currency,
                transaction_ref=proof.transaction_ref,
                status="settled",
            )


class MockRail(_BaseRail):
    """Рельс без сети — для тестов и офлайн-разработки.

    Существует не для красоты: он доказывает, что ядро не протекает ни в
    одну реальную сеть и что рельс действительно является адаптером.
    """

    name = "mock"

    def settle(self, proof: Proof, outcome: Decimal) -> Settlement:
        return Settlement(
            settled=settle_amount(proof.quote, outcome),
            currency=proof.quote.currency,
            transaction_ref=proof.transaction_ref,
            status="settled",
        )


_REGISTRY: dict[str, type] = {}


def register(rail_class: type) -> type:
    """Зарегистрировать адаптер. Добавление нового рельса — отдельный слайс
    с тестами и записью в docs/DECISIONS.md (Часть VIII.7, п.6)."""
    name = getattr(rail_class, "name", "")
    if not name or not isinstance(name, str):
        raise ValueError("адаптер обязан объявить строковый атрибут name")
    if name in _REGISTRY:
        raise ValueError(f"рельс {name!r} уже зарегистрирован")
    _REGISTRY[name] = rail_class
    return rail_class


for _cls in (X402Rail, MandateRail, MockRail):
    register(_cls)


def available() -> list[str]:
    return sorted(_REGISTRY)


def get(name: str) -> type:
    try:
        return _REGISTRY[name]
    except KeyError as exc:
        raise KeyError(
            f"неизвестный рельс {name!r}; доступны: {', '.join(available())}"
        ) from exc
