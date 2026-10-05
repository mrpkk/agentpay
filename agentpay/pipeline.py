"""Полный путь платного вызова: котировка → проверка → выполнение → расчёт.

Порядок зафиксирован и не переставляется:

1. котировка подписывается по каталогу;
2. авторизация проверяется — при `reject` ядро **не запускается**;
3. ядро выполняется, потребление измеряется;
4. сумма списывается по правилам схемы, маржа сверяется;
5. квитанция попадает в журнал.

Проверка до выполнения — не оптимизация, а правило: нельзя выполнить
платный вызов, за который никто не заплатил, даже если цена не сойдётся.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from decimal import Decimal
from hashlib import sha256
from threading import Lock
from typing import Any, Callable, Container, Mapping

from .catalog import Catalog
from .cores import CoreUnavailable, load_ailegal_core
from .ledger import DuplicateSettlement, Ledger, Receipt
from .measure import Measurement, measure, size_of
from .pricing import CostBreakdown
from .quote import SCHEME_UPTO, Quote, sign_quote
from .rails import Proof, Rail, get
from .verify import MemoryNonceStore, NonceStore, Verdict

__all__ = [
    "CallOutcome",
    "PaidCall",
    "UnpricedCatalog",
    "run_paid_call",
]


class UnpricedCatalog(RuntimeError):
    """Каталог не подтверждён: продавать по нему нельзя.

    При неподтверждённых тарифах себестоимость выходит нулевой, цена — нулевой,
    и продукт раздаётся бесплатно. Это тихая потеря выручки, поэтому
    обход защиты возможен только явным флагом, а не настройкой по умолчанию.
    """


@dataclass(frozen=True)
class CallOutcome:
    """Результат платного вызова целиком."""

    quote: Quote
    verdict: str
    result: Any
    measurement: Measurement
    cost: Decimal
    settled: Decimal
    receipt: Receipt | None
    executed: bool

    def as_dict(self) -> dict[str, Any]:
        document: dict[str, Any] = {
            "quote_id": self.quote.quote_id,
            "item": self.quote.item,
            "verdict": self.verdict,
            "executed": self.executed,
            "quoted_amount": str(self.quote.amount),
            "cost": str(self.cost),
            "settled": str(self.settled),
            "currency": self.quote.currency,
            "usage": self.measurement.as_dict(),
        }
        if self.receipt is not None:
            document["receipt"] = self.receipt.as_dict()
        return document


class PaidCall:
    """Денежный контур поверх каталога и рельса.

    Ядро передаётся вызывающим кодом: контур о нём ничего не знает и не
    подменяет. Единственное, что он делает, — измеряет и списывает.
    """

    def __init__(
        self,
        catalog: Catalog,
        rail_name: str,
        secret: bytes,
        *,
        ledger: Ledger | None = None,
        nonces: NonceStore | None = None,
        ttl_seconds: int = 300,
        **rail_kwargs: object,
    ) -> None:
        self.catalog = catalog
        self.pricelist = catalog.pricelist
        self.nonces = nonces if nonces is not None else MemoryNonceStore()
        self.rail: Rail = get(rail_name)(  # type: ignore[call-arg]
            self.pricelist, secret, nonces=self.nonces, **rail_kwargs
        )
        self.secret = secret
        self.ledger = ledger
        self.ttl_seconds = ttl_seconds
        # Один ресурс — одно исполнение. Проверка дубля и сама работа должны
        # быть под одной блокировкой: между ними параллельный вызов успевает
        # увидеть «ещё не рассчитано» и выполнить ту же работу повторно,
        # не заплатив за неё. Блокировка удерживается на всё время исполнения.
        self._keys: dict[str, Lock] = {}
        self._keys_guard = Lock()

    def _lock_for(self, quote_id: str) -> Lock:
        with self._keys_guard:
            lock = self._keys.get(quote_id)
            if lock is None:
                lock = Lock()
                self._keys[quote_id] = lock
            return lock

    def quote_for(
        self, item: str, resource: str, *, now: int | None = None
    ) -> Quote:
        """Котировка по каталогу со схемой позиции."""
        amount = self.pricelist.price_of(item)
        scheme = self.catalog.scheme_of(item)
        return sign_quote(
            quote_id=f"q-{sha256(resource.encode('utf-8')).hexdigest()[:16]}",
            item=item,
            amount=amount,
            currency=self.pricelist.currency,
            rail=self.rail.name,
            resource=resource,
            secret=self.secret,
            scheme=scheme,
            max_amount=(
                self.pricelist.upto(item) if scheme == SCHEME_UPTO else None
            ),
            ttl_seconds=self.ttl_seconds,
            now=now,
        )

    def run(
        self,
        item: str,
        resource: str,
        work: Callable[[], Any],
        *,
        extra_usage: Mapping[str, Decimal] | None = None,
        on_duplicate: str = "raise",
        allow_unverified: bool = False,
        now: int | None = None,
        available_providers: Container[str] = (),
    ) -> CallOutcome:
        """Полный цикл одного платного вызова."""
        # Провайдер проверяется до всего: до котировки и до захвата
        # авторизации. Иначе отказ пришлось бы оплачивать покупателю —
        # он заплатил бы за услугу, которую не оказали, и за попытку
        # её оказать.
        self.catalog.assert_executable(item, available_providers)
        if self.pricelist.unverified_rates() and not allow_unverified:
            raise UnpricedCatalog(
                "тарифы не подтверждены источником и датой: цена выведена из "
                "нулевой себестоимости и равна нулю. Заполните verified_on и "
                "source в каталоге либо передайте allow_unverified=True "
                "для локальных замеров."
            )
        quote = self.quote_for(item, resource, now=now)
        proof: Proof = self.rail.build_authorization(quote)

        # Решение принимает рельс, а не контур. Иначе лимит мандата и
        # одноразовость остались бы мёртвым кодом: контур проверял бы
        # подпись и цену сам, а то, что именно рельс считает своим
        # ограничением, никто бы не спросил.
        verdict = self.rail.authorize(
            proof, now=now, expected_resource=resource
        )
        if verdict is not Verdict.ACCEPT:
            return CallOutcome(
                quote=quote,
                verdict=verdict.value,
                result=None,
                measurement=Measurement(
                    usage=CostBreakdown(),
                    wall_seconds=Decimal(0),
                    label="не выполнено",
                ),
                cost=Decimal(0),
                settled=Decimal(0),
                receipt=None,
                executed=False,
            )
        # Повтор того же ресурса — это уже рассчитанный вызов. Проверять
        # это после исполнения нельзя: работа к тому моменту сделана, а
        # деньги за неё никто не заплатил, потому что квитанция не была
        # добавлена. Поэтому дубль отсекается до запуска ядра, под
        # блокировкой, удерживаемой до конца расчёта.
        with self._lock_for(quote.quote_id):
            existing = None
            if self.ledger is not None:
                existing = self.ledger.find(quote.quote_id)
                if existing is not None:
                    self.rail.release(proof)
                    if on_duplicate == "return":
                        return CallOutcome(
                            quote=quote,
                            verdict=existing.verdict,
                            result=None,
                            measurement=Measurement(
                                usage=CostBreakdown(),
                                wall_seconds=Decimal(0),
                                label="уже рассчитано",
                            ),
                            cost=Decimal(0),
                            settled=Decimal(0),
                            receipt=existing,
                            executed=False,
                        )
                    raise DuplicateSettlement(
                        f"котировка {quote.quote_id} уже рассчитана "
                        f"{existing.settled_amount} {existing.currency}"
                    )

            try:
                result, measurement = measure(work, label=item)
            except BaseException:
                # Ядро не отработало — авторизация не израсходована.
                # Освобождаем её, иначе покупатель заплатил бы за попытку,
                # а не за услугу.
                self.rail.release(proof)
                raise

            if extra_usage:
                measurement = measurement.with_usage(
                    **{k: Decimal(v) for k, v in extra_usage.items()}
                )
            measurement = measurement.with_usage(
                bytes_out=measurement.usage.bytes_out + size_of(result)
            )

            settlement = self.rail.settle(proof, measurement.usage.cpu_seconds)

            cost = measurement.cost_of(self.pricelist.rates)
            receipt = None
            if self.ledger is not None:
                revenue = settlement.settled
                receipt = self.ledger.append(
                    Receipt(
                        quote_id=quote.quote_id,
                        item=quote.item,
                        rail=quote.rail,
                        scheme=quote.scheme,
                        quoted_amount=str(quote.amount),
                        settled_amount=str(settlement.settled),
                        currency=quote.currency,
                        verdict=verdict.value,
                        cost=str(cost),
                        margin_at_settle=str(
                            (revenue - cost) / revenue
                            if revenue
                            else Decimal(0)
                        ),
                        usage=measurement.as_dict(),
                        result_digest=_digest(result),
                        issued_at=quote.issued_at,
                        settled_at=int(time.time()),
                    ),
                    on_duplicate=on_duplicate,
                )

            return CallOutcome(
                quote=quote,
                verdict=verdict.value,
                result=result,
                measurement=measurement,
                cost=cost,
                settled=settlement.settled,
                receipt=receipt,
                executed=True,
            )


def _digest(result: Any) -> str:
    import json

    try:
        payload = json.dumps(result, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        payload = str(result)
    return sha256(payload.encode("utf-8")).hexdigest()


def run_paid_call(
    catalog: Catalog,
    item: str,
    resource: str,
    secret: bytes,
    rail_name: str = "mock",
    *,
    ledger: Ledger | None = None,
    nonces: NonceStore | None = None,
    extra_usage: Mapping[str, Decimal] | None = None,
) -> CallOutcome:
    """Удобная обёртка: одноразовый платный вызов реального ядра AILegal."""
    core = load_ailegal_core()
    call = PaidCall(
        catalog, rail_name, secret, ledger=ledger, nonces=nonces
    )
    clauses: list[dict[str, Any]] = _clauses_for(item)
    return call.run(
        item,
        resource,
        lambda: core.clause_risk(clauses),
        extra_usage=extra_usage,
    )


def _clauses_for(item: str) -> list[dict[str, Any]]:
    if item.startswith("ailegal.clause"):
        return [{"type": "Confidentiality", "risk_level": "MEDIUM"}]
    raise CoreUnavailable(
        f"для позиции {item!r} нет подготовленного входа; "
        "передайте ядро явно через PaidCall.run"
    )
