"""Журнал расчётов: единственный источник правды о том, что кому выставлено.

Две задачи, которые не решаются логикой приложения:

1. **Прослеживаемость.** Каждый расчёт — строка JSONL с котировкой, замером,
   вердиктом и итогом. Разбор спора ведётся по файлу, а не по памяти.
2. **Идемпотентность.** Повторная попытка рассчитать одну котировку не должна
   списать деньги дважды. Повтор возвращает уже существующую квитанцию.

Файл только дописывается. Исправление ошибки — это компенсирующая запись,
а не переписывание истории.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from pathlib import Path
from threading import Lock
from typing import Any, Iterator

__all__ = ["Receipt", "Ledger", "DuplicateSettlement"]


class DuplicateSettlement(RuntimeError):
    """Котировка уже рассчитана. Повторное списание запрещено."""


@dataclass(frozen=True)
class Receipt:
    """Квитанция о расчёте."""

    quote_id: str
    item: str
    rail: str
    scheme: str
    quoted_amount: str
    settled_amount: str
    currency: str
    verdict: str
    cost: str
    margin_at_settle: str
    usage: dict[str, str] = field(default_factory=dict)
    result_digest: str = ""
    issued_at: int = 0
    settled_at: int = 0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, document: dict[str, Any]) -> "Receipt":
        known = {k: v for k, v in document.items() if k in cls.__annotations__}
        return cls(**known)


class Ledger:
    """Append-only журнал расчётов в формате JSONL."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()

    def __len__(self) -> int:
        return sum(1 for _ in self)

    def __iter__(self) -> Iterator[Receipt]:
        if not self.path.is_file():
            return iter(())
        receipts: list[Receipt] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            receipts.append(Receipt.from_dict(json.loads(line)))
        return iter(receipts)

    def find(self, quote_id: str) -> Receipt | None:
        for receipt in self:
            if receipt.quote_id == quote_id:
                return receipt
        return None

    def append(self, receipt: Receipt, *, on_duplicate: str = "raise") -> Receipt:
        """Записать квитанцию.

        `on_duplicate="return"` возвращает уже существующую запись вместо
        ошибки — так проверяется идемпотентность. `on_duplicate="raise"`
        (по умолчанию) запрещает двойное списание.

        Проверка дубликата и запись идут под одной блокировкой: по отдельности
        это две операции, между которыми параллельный расчёт успевает увидеть
        «свободно» и списать вторую квитанцию за тот же вызов.
        """
        with self._lock:
            existing = self.find(receipt.quote_id)
            if existing is not None:
                if on_duplicate == "return":
                    return existing
                raise DuplicateSettlement(
                    f"котировка {receipt.quote_id} уже рассчитана "
                    f"{existing.settled_amount} {existing.currency}"
                )
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(receipt.as_dict(), ensure_ascii=False) + "\n"
                )
            return receipt

    def total(self) -> Decimal:
        total = Decimal(0)
        for receipt in self:
            total += Decimal(receipt.settled_amount)
        return total

    def total_by_item(self) -> dict[str, Decimal]:
        totals: dict[str, Decimal] = {}
        for receipt in self:
            totals[receipt.item] = totals.get(
                receipt.item, Decimal(0)
            ) + Decimal(receipt.settled_amount)
        return totals

    def margin(self) -> Decimal | None:
        """Фактическая маржа по всем расчётам журнала."""
        revenue = Decimal(0)
        cost = Decimal(0)
        for receipt in self:
            revenue += Decimal(receipt.settled_amount)
            cost += Decimal(receipt.cost)
        if revenue == 0:
            return None
        return (revenue - cost) / revenue

    def summary(self) -> dict[str, Any]:
        margin = self.margin()
        return {
            "path": str(self.path),
            "receipts": len(self),
            "total": str(self.total()),
            "margin": str(margin) if margin is not None else None,
            "by_item": {
                item: str(amount)
                for item, amount in sorted(self.total_by_item().items())
            },
        }
