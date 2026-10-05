"""Измерение фактического потребления вызова.

Смысл «справедливого распределения ресурсов» проверяема только тогда, когда
потребление измерено, а не объявлено. Модуль измеряет то, что действительно
произошло: процессорное время, размер входа и выхода, число внешних вызовов.
Ничего не оценивается «на глаз».

Внешние зависимости отсутствуют намеренно: измерение должно работать там же,
где и расчёт, иначе замер нельзя будет повторить при разборе инцидента.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any, Callable, TypeVar

from .pricing import CostBreakdown, RateCard

__all__ = ["Measurement", "measure", "size_of"]

T = TypeVar("T")

_SECONDS_PER_HOUR = Decimal(3600)


@dataclass(frozen=True)
class Measurement:
    """Результат одного замера: потребление плюс стоимость по тарифам."""

    usage: CostBreakdown
    wall_seconds: Decimal
    label: str = ""

    def cost_of(self, rates: RateCard) -> Decimal:
        return rates.cost_of(self.usage)

    def with_usage(self, **changes: Decimal) -> "Measurement":
        return replace(self, usage=replace(self.usage, **changes))

    def as_dict(self) -> dict[str, str]:
        return {
            "tokens": str(self.usage.tokens),
            "cpu_seconds": str(self.usage.cpu_seconds),
            "bytes_out": str(self.usage.bytes_out),
            "storage_writes": str(self.usage.storage_writes),
            "external_calls": str(self.usage.external_calls),
            "wall_seconds": str(self.wall_seconds),
            "label": self.label,
        }


def size_of(value: Any) -> Decimal:
    """Размер значения в байтах: для текста — в кодировке UTF-8."""
    if value is None:
        return Decimal(0)
    if isinstance(value, bytes):
        return Decimal(len(value))
    if isinstance(value, str):
        return Decimal(len(value.encode("utf-8")))
    return Decimal(len(json.dumps(value, ensure_ascii=False, default=str).encode("utf-8")))


def measure(
    call: Callable[[], T],
    *,
    label: str = "",
    clock: Callable[[], float] = time.process_time,
) -> tuple[T, Measurement]:
    """Выполнить `call` и измерить процессорное время.

    Измеряется именно процессорное время, а не настенные часы: ожидание сети
    не является нашим расходом и не должно попадать в себестоимость вычислений.
    """
    started = clock()
    wall_started = time.perf_counter()
    try:
        result = call()
    finally:
        cpu_seconds = Decimal(str(max(0.0, clock() - started)))
        wall_seconds = Decimal(str(max(0.0, time.perf_counter() - wall_started)))
    measurement = Measurement(
        usage=CostBreakdown(cpu_seconds=cpu_seconds),
        wall_seconds=wall_seconds,
        label=label,
    )
    return result, measurement
