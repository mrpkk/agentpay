"""Каталог как данные: `pricelist.toml`, а не код.

Цена не должна жить в программе. Иначе её нельзя пересчитать, показать
продавцу и оспорить. Каталог лежит в файле рядом с продуктом, содержит
себестоимость по каждому вызову и тарифы с пометкой проверки.

Формат TOML читается стандартной библиотекой (`tomllib`), JSON — тоже.
Зависимостей у пакета нет и не появится.
"""

from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Container, Mapping

from .pricing import (
    CostBreakdown,
    PriceItem,
    PriceList,
    RateCard,
    TARGET_MARGIN,
)
from .quote import SCHEME_EXACT, SCHEME_UPTO

__all__ = [
    "ItemMeta",
    "Catalog",
    "ProviderUnavailable",
    "load_catalog",
    "load_pricelist",
]


class ProviderUnavailable(RuntimeError):
    """Позиция требует внешнего провайдера, а он не подключён.

    Продавать такой вызов нельзя: покупатель платит за услугу, которую
    никто не оказал. Позиция с `requires_provider` обязана либо иметь
    настоящего провайдера, либо не продаваться вовсе — молчаливая подмена
    офлайн-работы другой работой здесь равносильна обману.
    """


@dataclass(frozen=True)
class ItemMeta:
    """Метаданные позиции, которые не относятся к арифметике цены."""

    scheme: str = SCHEME_EXACT
    tags: tuple[str, ...] = ()
    requires_provider: str = ""
    """Имя внешнего провайдера, если вызов без сети невозможен.

    Заполняется честно: позиция, которой нужен LLM-провайдер, не должна
    выглядеть исполняемой офлайн.
    """

    def __post_init__(self) -> None:
        if self.scheme not in (SCHEME_EXACT, SCHEME_UPTO):
            raise ValueError(f"неизвестная схема: {self.scheme!r}")


@dataclass(frozen=True)
class Catalog:
    """Каталог цен вместе с метаданными позиций."""

    pricelist: PriceList
    meta: Mapping[str, ItemMeta]
    source_path: str = ""

    def item_meta(self, name: str) -> ItemMeta:
        return self.meta.get(name, ItemMeta())

    def scheme_of(self, name: str) -> str:
        return self.item_meta(name).scheme

    def provider_of(self, name: str) -> str:
        """Имя внешнего провайдера, необходимого позиции, либо пустая строка."""
        return self.item_meta(name).requires_provider

    def assert_executable(
        self, name: str, available: Container[str] = ()
    ) -> None:
        """Отказать, если позиции нужен провайдер, которого нет.

        `available` — имена реально подключённых провайдеров. Пустое множество
        означает «подключено только то, что работает офлайн», поэтому позиция
        с `requires_provider` не проходит. Само наличие поля в каталоге —
        декларация, а не проверка; без этого вызова она оставалась бы
        вопиющей ложью: деньги за извлечение клауз брались, а клаузы не
        извлекались.
        """
        provider = self.provider_of(name)
        if not provider:
            return
        if provider in available:
            return
        raise ProviderUnavailable(
            f"позиция {name!r} требует провайдера {provider!r}, который не "
            f"подключён (доступно: {', '.join(sorted(available)) or 'ничего'}). "
            f"Продавать такой вызов нельзя: деньги будут списаны за услугу, "
            f"которую никто не оказал. Подключите провайдера или уберите "
            f"позицию из каталога."
        )

    def offline_items(self) -> tuple[str, ...]:
        """Позиции, исполнимые без внешнего провайдера."""
        return tuple(
            item.name
            for item in self.pricelist.items
            if not self.item_meta(item.name).requires_provider
        )

    def provider_items(self) -> tuple[str, ...]:
        return tuple(
            item.name
            for item in self.pricelist.items
            if self.item_meta(item.name).requires_provider
        )

    def report(self) -> dict[str, Any]:
        """Диагностика каталога для CLI и для KILL-GATE K4."""
        return {
            "source": self.source_path,
            "currency": self.pricelist.currency,
            "market_origin": self.pricelist.market_origin,
            "target_margin": str(self.pricelist.target_margin),
            "minor_unit": str(self.pricelist.minor_unit),
            "rates_verified": self.pricelist.rates.is_verified,
            "rates_source": self.pricelist.rates.source,
            "rates_verified_on": self.pricelist.rates.verified_on,
            "items": {
                item.name: {
                    "cost": str(self.pricelist.cost_of(item.name)),
                    "price": str(self.pricelist.price_of(item.name)),
                    "margin": str(self.pricelist.margin_of(item.name)),
                    "scheme": self.scheme_of(item.name),
                    "requires_provider": self.item_meta(
                        item.name
                    ).requires_provider,
                    "offline": item.name in self.offline_items(),
                }
                for item in self.pricelist.items
            },
        }


def _decimal(value: Any, field: str) -> Decimal:
    if value is None:
        return Decimal(0)
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"поле {field!r} не является числом: {value!r}") from exc


def _rate_card(raw: Mapping[str, Any]) -> RateCard:
    return RateCard(
        usd_per_million_tokens=_decimal(
            raw.get("usd_per_million_tokens"), "rates.usd_per_million_tokens"
        ),
        usd_per_cpu_hour=_decimal(
            raw.get("usd_per_cpu_hour"), "rates.usd_per_cpu_hour"
        ),
        usd_per_megabyte_out=_decimal(
            raw.get("usd_per_megabyte_out"), "rates.usd_per_megabyte_out"
        ),
        usd_per_storage_write=_decimal(
            raw.get("usd_per_storage_write"), "rates.usd_per_storage_write"
        ),
        usd_per_external_call=_decimal(
            raw.get("usd_per_external_call"), "rates.usd_per_external_call"
        ),
        verified_on=str(raw.get("verified_on", "")),
        source=str(raw.get("source", "")),
    )


def _usage(raw: Mapping[str, Any]) -> CostBreakdown:
    return CostBreakdown(
        tokens=_decimal(raw.get("tokens"), "tokens"),
        cpu_seconds=_decimal(raw.get("cpu_seconds"), "cpu_seconds"),
        bytes_out=_decimal(raw.get("bytes_out"), "bytes_out"),
        storage_writes=_decimal(raw.get("storage_writes"), "storage_writes"),
        external_calls=_decimal(raw.get("external_calls"), "external_calls"),
    )


def _from_mapping(
    document: Mapping[str, Any], source_path: str
) -> Catalog:
    raw_items = document.get("items") or ()
    if not isinstance(raw_items, (list, tuple)):
        raise ValueError("items должен быть массивом")

    items: list[PriceItem] = []
    meta: dict[str, ItemMeta] = {}
    for raw in raw_items:
        name = str(raw.get("name", "")).strip()
        if not name:
            raise ValueError("у позиции отсутствует name")
        items.append(
            PriceItem(
                name=name,
                usage=_usage(raw),
                description=str(raw.get("description", "")),
                tags=tuple(str(t) for t in raw.get("tags", ())),
            )
        )
        meta[name] = ItemMeta(
            scheme=str(raw.get("scheme", SCHEME_EXACT)),
            tags=tuple(str(t) for t in raw.get("tags", ())),
            requires_provider=str(raw.get("requires_provider", "")),
        )

    pricelist = PriceList(
        currency=str(document.get("currency", "")),
        items=tuple(items),
        rates=_rate_card(document.get("rates") or {}),
        target_margin=_decimal(
            document.get("target_margin"), "target_margin"
        )
        or TARGET_MARGIN,
        minor_unit=_decimal(document.get("minor_unit"), "minor_unit")
        or Decimal("0.01"),
        market_origin=str(document.get("market_origin", "INT")),
    )
    return Catalog(pricelist=pricelist, meta=meta, source_path=source_path)


def load_catalog(path: str | Path) -> Catalog:
    """Прочитать каталог из TOML или JSON."""
    file = Path(path)
    if not file.is_file():
        raise FileNotFoundError(f"каталог не найден: {file}")
    text = file.read_text(encoding="utf-8")
    if file.suffix.lower() == ".json":
        document = json.loads(text)
    else:
        document = tomllib.loads(text)
    if not isinstance(document, Mapping):
        raise ValueError(f"каталог {file} должен быть объектом")
    return _from_mapping(document, str(file))


def load_pricelist(path: str | Path) -> PriceList:
    """Только прайс-лист без метаданных."""
    return load_catalog(path).pricelist
