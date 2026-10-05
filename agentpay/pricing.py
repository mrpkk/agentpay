"""Модель амортизации вычислений — Часть VIII.3 роадмапа v4.

Маржа определяется как (цена − себестоимость) / цена. Отсюда цена при
целевой марже m: цена = себестоимость / (1 − m). Делить на саму маржу
нельзя: с / 0.60 даёт маржу 0.40, а не 0.60.

Цена округляется вверх до минимальной денежной единицы, поэтому фактическая
маржа никогда не опускается ниже целевой. Все входные тарифы помечаются
PRICE TO VERIFY, потому что рынок меняется быстрее документации.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_CEILING, Decimal, getcontext

getcontext().prec = 28

__all__ = [
    "CostBreakdown",
    "RateCard",
    "PriceItem",
    "PriceList",
    "TARGET_MARGIN",
    "PRICE_TO_VERIFY",
]

TARGET_MARGIN = Decimal("0.60")
"""Минимальная целевая маржа. Ниже — срабатывает KILL-GATE K4."""

PRICE_TO_VERIFY = "PRICE TO VERIFY"
"""Пометка: тариф требует подтверждения источником и датой."""


@dataclass(frozen=True)
class CostBreakdown:
    """Себестоимость одного вызова, разложенная по компонентам.

    Каждый компонент — производная от измеримой величины, а не от оценки.
    Это и есть «честное распределение ресурсов»: пользователь платит ровно
    за то, что израсходовано.
    """

    tokens: Decimal = Decimal(0)
    cpu_seconds: Decimal = Decimal(0)
    bytes_out: Decimal = Decimal(0)
    storage_writes: Decimal = Decimal(0)
    external_calls: Decimal = Decimal(0)

    def __post_init__(self) -> None:
        for field_name in (
            "tokens",
            "cpu_seconds",
            "bytes_out",
            "storage_writes",
            "external_calls",
        ):
            value = getattr(self, field_name)
            if value < 0:
                raise ValueError(
                    f"компонент {field_name!r} отрицателен: {value}"
                )


@dataclass(frozen=True)
class RateCard:
    """Тарифы внешних ресурсов. Каждый тариф — с датой и пометкой."""

    usd_per_million_tokens: Decimal = Decimal(0)
    usd_per_cpu_hour: Decimal = Decimal(0)
    usd_per_megabyte_out: Decimal = Decimal(0)
    usd_per_storage_write: Decimal = Decimal(0)
    usd_per_external_call: Decimal = Decimal(0)
    verified_on: str = ""
    source: str = ""

    def __post_init__(self) -> None:
        for field_name, value in vars(self).items():
            if field_name in {"verified_on", "source"}:
                continue
            if not isinstance(value, Decimal):
                raise TypeError(f"{field_name!r} должен быть Decimal")
            if value < 0:
                raise ValueError(f"тариф {field_name!r} отрицателен: {value}")

    def cost_of(self, usage: CostBreakdown) -> Decimal:
        """Себестоимость = сумма произведений объём × тариф."""
        return (
            (usage.tokens / Decimal(1_000_000)) * self.usd_per_million_tokens
            + (usage.cpu_seconds / Decimal(3600)) * self.usd_per_cpu_hour
            + (usage.bytes_out / Decimal(1_000_000)) * self.usd_per_megabyte_out
            + usage.storage_writes * self.usd_per_storage_write
            + usage.external_calls * self.usd_per_external_call
        )

    @property
    def is_verified(self) -> bool:
        """Тариф подтверждён источником и датой — иначе это PRICE TO VERIFY."""
        return bool(self.verified_on.strip()) and bool(self.source.strip())


@dataclass(frozen=True)
class PriceItem:
    """Единица товара: именованный вызов с известной себестоимостью."""

    name: str
    usage: CostBreakdown
    description: str = ""
    tags: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("name обязателен")


@dataclass(frozen=True)
class PriceList:
    """Каталог цен, привязанный к тарифам и целевой марже.

    Ядро продукта не импортирует SDK платёжного рельса: цена живёт здесь,
    рельс только проверяет и переносит (Часть VIII.7, правило 4).
    """

    currency: str
    items: tuple[PriceItem, ...]
    rates: RateCard
    target_margin: Decimal = TARGET_MARGIN
    minor_unit: Decimal = Decimal("0.01")
    market_origin: str = "INT"

    def __post_init__(self) -> None:
        if not self.currency.strip():
            raise ValueError("currency обязателен")
        if not 0 < self.target_margin < 1:
            raise ValueError(
                f"target_margin должен быть в (0, 1), получено {self.target_margin}"
            )
        if self.minor_unit <= 0:
            raise ValueError("minor_unit должен быть положительным")
        seen: set[str] = set()
        for item in self.items:
            if item.name in seen:
                raise ValueError(f"дубликат позиции: {item.name!r}")
            seen.add(item.name)

    def item(self, name: str) -> PriceItem:
        for candidate in self.items:
            if candidate.name == name:
                return candidate
        raise KeyError(f"позиция не найдена: {name!r}")

    def cost_of(self, name: str) -> Decimal:
        """Себестоимость позиции в валюте каталога."""
        return self.rates.cost_of(self.item(name).usage)

    def price_of(self, name: str) -> Decimal:
        """Отпускная цена = ceil(себестоимость / (1 − маржа)) в мин. единицах.

        Округление строго вверх: цена никогда не опускается ниже целевой
        маржи, даже на микроскопическом вызове.
        """
        cost = self.cost_of(name)
        if cost == 0:
            return Decimal(0)
        raw = cost / (Decimal(1) - self.target_margin)
        return raw.quantize(self.minor_unit, rounding=ROUND_CEILING)

    def upto(self, name: str) -> Decimal:
        """Потолок для схемы `upto` (pay-per-inference, AWS AgentCore).

        Фактическая плата может быть ниже потолка, но никогда не выше.
        """
        return self.price_of(name)

    def margin_of(self, name: str) -> Decimal:
        """Доля маржи при отпускной цене."""
        price = self.price_of(name)
        if price == 0:
            return Decimal(0)
        return (price - self.cost_of(name)) / price

    def health(self) -> dict[str, Decimal]:
        """Диагностика: маржа по каждой позиции — вход для KILL-GATE K4."""
        return {item.name: self.margin_of(item.name) for item in self.items}

    def unverified_rates(self) -> bool:
        """True — тарифы не подтверждены источником и датой."""
        return not self.rates.is_verified
