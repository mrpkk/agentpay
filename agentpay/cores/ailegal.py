"""Адаптеры к реальным детерминированным ядрам.

Ключевое правило этого модуля: **заглушек здесь нет**. Если ядро AILegal
недоступно, вызов падает с явной ошибкой, а не возвращает правдоподобный
ответ. Имитация результата в денежном пути — это ложь, которая всплывёт на
сверке с клиентом.

Ядра загружаются по файловому пути, а не через пакет AILegal: его модули
лежат внутри FastAPI-приложения с десятками зависимостей, а
`clause_dna.py` и `risk_aggregation.py` — чистый Python без единого
импорта. Загрузка файлом не копирует логику и не расходится с оригиналом.
"""

from __future__ import annotations

import importlib.util
import json
import os
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, Sequence

__all__ = [
    "CoreUnavailable",
    "AilegalCore",
    "AILEGAL_ROOT_ENV",
    "DEFAULT_AILEGAL_ROOT",
    "ailegal_root",
    "load_ailegal_core",
]

AILEGAL_ROOT_ENV = "AILEGAL_ROOT"
DEFAULT_AILEGAL_ROOT = "/home/iamthat/AILegal/backend/app/services"

_CLAUSE_DNA = "clause_dna.py"
_RISK_AGGREGATION = "risk_aggregation.py"
_GOLDEN_CORPUS = Path("/home/iamthat/AILegal/backend/corpus/golden.json")


class CoreUnavailable(RuntimeError):
    """Реальное ядро не найдено. Заглушка не подставляется."""


def _load_module(path: Path, name: str) -> ModuleType:
    if not path.is_file():
        raise CoreUnavailable(
            f"модуль ядра не найден: {path}. Заглушка не подставляется — "
            f"задайте корень через {AILEGAL_ROOT_ENV}."
        )
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise CoreUnavailable(f"не удалось загрузить модуль: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def ailegal_root() -> Path:
    return Path(os.environ.get(AILEGAL_ROOT_ENV, DEFAULT_AILEGAL_ROOT))


@dataclass(frozen=True)
class AilegalCore:
    """Реальные ClauseDNA 3.0 и Risk Aggregation 2.0 из AILegal."""

    clause_dna: ModuleType
    risk_aggregation: ModuleType
    root: str

    @property
    def engine(self) -> str:
        return "document_risk_v2"

    def clause_types(self) -> tuple[str, ...]:
        return tuple(self.clause_dna.CLAUSE_TYPES)

    def categories(self) -> tuple[str, ...]:
        return tuple(self.clause_dna.CATEGORIES)

    def categorize(self, clause: dict[str, Any]) -> str:
        """Категория клаузы по таблице AILegal, без догадок."""
        key = clause.get("type") or clause.get("clause_type") or ""
        return self.clause_dna.CLAUSE_CATEGORY.get(key, "PROCEDURAL")

    def clause_risk(
        self,
        clauses: Sequence[dict[str, Any]],
        compliance_status: str | None = None,
    ) -> dict[str, Any]:
        """Детерминированная агрегация риска документа.

        Каузы обогащаются категориями, если их ещё нет, — ровно так же, как
        в `ClauseExtractor` AILegal, чтобы результат совпадал с продуктом.
        """
        enriched: list[dict[str, Any]] = []
        for clause in clauses:
            item = dict(clause)
            if not item.get("category"):
                item["category"] = self.categorize(item)
            enriched.append(item)
        return self.risk_aggregation.document_risk_v2(
            enriched, compliance_status
        )

    def run_golden(self, limit: int | None = None) -> dict[str, Any]:
        """Прогнать реальный золотой корпус AILegal через реальное ядро."""
        if not _GOLDEN_CORPUS.is_file():
            raise CoreUnavailable(f"золотой корпус не найден: {_GOLDEN_CORPUS}")
        document = json.loads(_GOLDEN_CORPUS.read_text(encoding="utf-8"))
        cases = document.get("cases", [])
        if limit is not None:
            cases = cases[:limit]
        checked = 0
        mismatches: list[dict[str, Any]] = []
        for case in cases:
            verdict = self.clause_risk(
                case.get("clauses", []),
                case.get("compliance_status"),
            )
            expected = case.get("expected_level")
            if expected is None:
                continue
            checked += 1
            if verdict.get("risk_level") != expected:
                mismatches.append(
                    {
                        "id": case.get("id"),
                        "title": case.get("title"),
                        "compliance_status": case.get("compliance_status"),
                        "expected": expected,
                        "actual": verdict.get("risk_level"),
                        "score": verdict.get("risk_score"),
                    }
                )
        return {
            "corpus": str(_GOLDEN_CORPUS),
            "schema": document.get("meta", {}).get("schema", ""),
            "engine": self.engine,
            "cases": len(cases),
            "checked": checked,
            "mismatches": mismatches,
            "accuracy": (
                Decimal_ratio(checked - len(mismatches), checked)
                if checked
                else None
            ),
        }


def Decimal_ratio(numerator: int, denominator: int):
    from decimal import Decimal

    if denominator == 0:
        return Decimal(0)
    return Decimal(numerator) / Decimal(denominator)


def load_ailegal_core(root: str | Path | None = None) -> AilegalCore:
    """Загрузить реальные модули AILegal. Бросает CoreUnavailable, если их нет."""
    base = Path(root) if root is not None else ailegal_root()
    clause_dna = _load_module(base / _CLAUSE_DNA, "ailegal_clause_dna")
    risk_aggregation = _load_module(
        base / _RISK_AGGREGATION, "ailegal_risk_aggregation"
    )
    for attribute in ("CLAUSE_TYPES", "CLAUSE_CATEGORY", "CATEGORIES"):
        if not hasattr(clause_dna, attribute):
            raise CoreUnavailable(
                f"в {base / _CLAUSE_DNA} нет атрибута {attribute}: "
                "структура AILegal изменилась, нужен разбор"
            )
    if not hasattr(risk_aggregation, "document_risk_v2"):
        raise CoreUnavailable(
            f"в {base / _RISK_AGGREGATION} нет document_risk_v2: "
            "структура AILegal изменилась, нужен разбор"
        )
    return AilegalCore(
        clause_dna=clause_dna,
        risk_aggregation=risk_aggregation,
        root=str(base),
    )
