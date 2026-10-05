"""CLI agentpay: цена, котировка, проверка авторизации, расчёт.

Тот же приём, что и в attest: сначала `quote --verify`, чтобы доказать, что
котировка не подделана и не просрочена, и только потом `quote --settle`.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from decimal import Decimal
from typing import Sequence

from .catalog import ProviderUnavailable, load_catalog
from .cores import load_ailegal_core
from .pricing import CostBreakdown, PriceItem, PriceList, RateCard
from .quote import SCHEME_EXACT, SCHEME_UPTO, Quote, sign_quote
from .rails import MockRail, available, get
from .verify import MemoryNonceStore, Verdict

__all__ = ["main", "build_parser", "demo_pricelist"]

_SECRET_ENV = "AGENTPAY_SECRET"
_VERIFY_SECRET_ENV = "AGENTPAY_VERIFY_SECRET"
_SALT_ENV = "AGENTPAY_SALT"


def demo_pricelist() -> PriceList:
    """Каталог для самопроверки. Тарифы помечены как PRICE TO VERIFY:
    это пример структуры себестоимости, а не рыночные цены."""
    rates = RateCard(
        usd_per_million_tokens=Decimal("0.30"),
        usd_per_cpu_hour=Decimal("0.05"),
        usd_per_megabyte_out=Decimal("0.09"),
        verified_on="",
        source="PLACEHOLDER — заменить на тариф с источником и датой",
    )
    items = (
        PriceItem(
            name="clause.extract",
            usage=CostBreakdown(
                tokens=Decimal("18000"),
                cpu_seconds=Decimal("0.8"),
                bytes_out=Decimal("24000"),
            ),
            description="Извлечение условий из договора",
        ),
        PriceItem(
            name="defi.snapshot",
            usage=CostBreakdown(
                cpu_seconds=Decimal("0.3"),
                external_calls=Decimal(3),
            ),
            description="Снимок состояния протокола",
        ),
        PriceItem(
            name="solidity.verify",
            usage=CostBreakdown(
                cpu_seconds=Decimal("4"),
                bytes_out=Decimal("120000"),
                storage_writes=Decimal(1),
            ),
            description="Верификация Solidity-контракта",
        ),
    )
    return PriceList(
        currency="USD",
        items=items,
        rates=rates,
        market_origin="INT",
    )


def _secret(raw: str | None) -> bytes:
    """Секрет из аргумента или окружения; в проде — только окружение."""
    if raw:
        return raw.encode("utf-8")
    from_env = os.environ.get(_SECRET_ENV)
    if not from_env:
        raise SystemExit(
            f"нужен секрет подписи: --secret или переменная {_SECRET_ENV}"
        )
    return from_env.encode("utf-8")


def _resource(raw: str) -> str:
    """Адрес авторизации по умолчанию — детерминированный, не случайный."""
    from hashlib import sha256

    from_env = os.environ.get(_SALT_ENV)
    if not from_env:
        raise SystemExit(
            f"нужен идентификатор продавца: --resource или переменная {_SALT_ENV}"
        )
    digest = sha256(f"{from_env}:{raw}".encode("utf-8")).hexdigest()
    return f"urn:agentpay:{raw}:{digest[:16]}"


def _emit(payload: dict[str, object]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agentpay",
        description="rail-agnostic цена и авторизация вызовов агентов",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    catalog = sub.add_parser("price", help="показать каталог и диагностику маржи")
    catalog.add_argument("--format", choices=("text", "json"), default="text")

    rails = sub.add_parser("rails", help="показать доступные рельс-адаптеры")

    quote = sub.add_parser("quote", help="выдать котировку и проверить её")
    quote.add_argument("item")
    quote.add_argument("--rail", default="mock", choices=available())
    quote.add_argument("--secret", help="ключ выпуска котировки")
    quote.add_argument(
        "--verify-secret",
        help="ключ проверки; по умолчанию равен --secret. Разные ключи "
        "нужны, чтобы проверить подделку: продавец подписывает, рельс "
        "верифицирует своим ключом",
    )
    quote.add_argument("--resource", default="seller")
    quote.add_argument(
        "--scheme", choices=(SCHEME_EXACT, SCHEME_UPTO), default=SCHEME_EXACT
    )
    quote.add_argument("--ttl", type=int, default=300)
    quote.add_argument(
        "--verify", action="store_true", help="проверить подпись и срок"
    )
    quote.add_argument(
        "--settle", type=str, default=None, metavar="ACTUAL",
        help="рассчитать фактическое списание (например 0.012)",
    )
    quote.add_argument("--nonce-store", default="memory")

    catalog = sub.add_parser(
        "catalog", help="показать каталог из файла с диагностикой"
    )
    catalog.add_argument("path", nargs="?", default="pricelist/ailegal.example.toml")
    catalog.add_argument("--format", choices=("text", "json"), default="text")

    golden = sub.add_parser(
        "golden", help="прогнать золотой корпус AILegal через реальное ядро"
    )
    golden.add_argument("--limit", type=int, default=None)
    golden.add_argument("--format", choices=("text", "json"), default="text")

    run = sub.add_parser(
        "run", help="полный путь: котировка → проверка → ядро → расчёт → квитанция"
    )
    run.add_argument("item")
    run.add_argument("--catalog", default="pricelist/ailegal.example.toml")
    run.add_argument("--rail", default="mock", choices=available())
    run.add_argument("--secret")
    run.add_argument("--resource", default="ailegal")
    run.add_argument(
        "--ledger", default=None, help="путь к журналу расчётов (JSONL)"
    )
    run.add_argument(
        "--allow-unverified",
        action="store_true",
        help="разрешить расчёт по неподтверждённым тарифам (только замеры)",
    )
    run.add_argument(
        "--on-duplicate",
        choices=("raise", "return"),
        default="raise",
        help="поведение при повторном расчёте той же котировки",
    )
    run.add_argument(
        "--clauses",
        default=None,
        help="JSON со списком клауз для реального вызова ядра",
    )

    ledger = sub.add_parser("ledger", help="сводка по журналу расчётов")
    ledger.add_argument("path")
    ledger.add_argument("--format", choices=("text", "json"), default="text")
    return parser


def _cmd_price(args: argparse.Namespace) -> int:
    pricelist = demo_pricelist()
    if args.format == "json":
        _emit(
            {
                "currency": pricelist.currency,
                "target_margin": str(pricelist.target_margin),
                "rates_verified": pricelist.rates.is_verified,
                "prices": {
                    item.name: {
                        "cost": str(pricelist.cost_of(item.name)),
                        "price": str(pricelist.price_of(item.name)),
                        "upto": str(pricelist.upto(item.name)),
                        "margin": str(pricelist.margin_of(item.name)),
                    }
                    for item in pricelist.items
                },
            }
        )
        return 0
    print(f"каталог {pricelist.currency} · маржа {pricelist.target_margin}")
    if pricelist.unverified_rates():
        print("ВНИМАНИЕ: тарифы не подтверждены источником и датой (PRICE TO VERIFY)")
    for item in pricelist.items:
        print(
            f"  {item.name:<18} себестоимость {pricelist.cost_of(item.name):>10} "
            f"цена {pricelist.price_of(item.name):>8} "
            f"маржа {pricelist.margin_of(item.name) * 100:.1f}%"
        )
    return 0


def _cmd_rails(_args: argparse.Namespace) -> int:
    for name in available():
        print(name)
    return 0


def _cmd_quote(args: argparse.Namespace) -> int:
    pricelist = demo_pricelist()
    secret = _secret(args.secret)
    verify_raw = args.verify_secret or os.environ.get(_VERIFY_SECRET_ENV)
    verify_secret = verify_raw.encode("utf-8") if verify_raw else secret
    resource = _resource(args.resource)

    try:
        amount = pricelist.price_of(args.item)
    except KeyError as exc:
        print(f"позиция не найдена: {args.item}", file=sys.stderr)
        print(f"доступны: {', '.join(i.name for i in pricelist.items)}", file=sys.stderr)
        return 2

    quote = sign_quote(
        quote_id=f"q-{resource[-16:]}",
        item=args.item,
        amount=amount,
        currency=pricelist.currency,
        rail=args.rail,
        resource=resource,
        secret=secret,
        scheme=args.scheme,
        max_amount=pricelist.upto(args.item) if args.scheme == SCHEME_UPTO else None,
        ttl_seconds=args.ttl,
    )

    rail_class = get(args.rail)
    rail = rail_class(pricelist, verify_secret, nonces=MemoryNonceStore())
    proof = rail.build_authorization(quote)

    result: dict[str, object] = {
        "quote_id": quote.quote_id,
        "item": quote.item,
        "amount": str(quote.amount),
        "currency": quote.currency,
        "rail": quote.rail,
        "scheme": quote.scheme,
        "resource": quote.resource,
        "nonce": quote.nonce,
        "expires_at": quote.expires_at,
        "digest": quote.digest(),
        "signature": quote.signature,
    }

    if args.verify:
        verdict = rail.verify_authorization(proof)
        result["verdict"] = verdict.value
        result["signed"] = quote.signature_valid(verify_secret)
        if verdict is not Verdict.ACCEPT:
            _emit(result)
            return 1

    if args.settle is not None:
        actual = Decimal(args.settle)
        if actual > quote.ceiling:
            result["warning"] = (
                f"фактическое потребление {actual} выше потолка {quote.ceiling}; "
                "списано по потолку, превышение — в метрику"
            )
        settlement = rail.settle(proof, actual)
        result["settlement"] = {
            "settled": str(settlement.settled),
            "currency": settlement.currency,
            "status": settlement.status,
            "transaction_ref": settlement.transaction_ref,
        }

    _emit(result)
    return 0


def _cmd_catalog(args: argparse.Namespace) -> int:
    catalog = load_catalog(args.path)
    if args.format == "json":
        _emit(catalog.report())
        return 0
    report = catalog.report()
    print(
        f"{report['source']} · {report['currency']} · "
        f"маржа {report['target_margin']} · мин. единица {report['minor_unit']}"
    )
    if not report["rates_verified"]:
        print(
            f"ВНИМАНИЕ PRICE TO VERIFY: тарифы не подтверждены "
            f"(source: {report['rates_source']!r}, "
            f"verified_on: {report['rates_verified_on']!r})"
        )
    for name, row in report["items"].items():
        mark = "офлайн" if row["offline"] else f"нужен {row['requires_provider']}"
        print(
            f"  {name:<28} себестоимость {row['cost']:>10} "
            f"цена {row['price']:>7} маржа {row['margin'][:6]:>6} "
            f"[{row['scheme']}, {mark}]"
        )
    return 0


def _cmd_golden(args: argparse.Namespace) -> int:
    core = load_ailegal_core()
    report = core.run_golden(limit=args.limit)
    if args.format == "json":
        _emit(report)
    else:
        print(f"корпус: {report['corpus']}")
        print(f"схема: {report['schema']} · движок: {report['engine']}")
        print(f"кейсов: {report['cases']} · проверено: {report['checked']}")
        print(f"точность: {report['accuracy']}")
        for item in report["mismatches"]:
            print(
                f"  РАСХОЖДЕНИЕ {item['id']} {item['title']}: "
                f"ожидалось {item['expected']}, получено {item['actual']}"
            )
    return 0 if not report["mismatches"] else 1


def _cmd_run(args: argparse.Namespace) -> int:
    from .cores import CoreUnavailable
    from .ledger import DuplicateSettlement, Ledger
    from .pipeline import PaidCall, UnpricedCatalog

    catalog = load_catalog(args.catalog)
    secret = _secret(args.secret)
    core = load_ailegal_core()
    ledger = Ledger(args.ledger) if args.ledger else None

    clauses: list[dict[str, object]]
    if args.clauses:
        clauses = json.loads(args.clauses)
    else:
        clauses = [
            {"type": "Confidentiality", "risk_level": "MEDIUM"},
            {"type": "Limitation of Liability", "risk_level": "HIGH"},
            {"type": "Termination", "risk_level": "CRITICAL"},
        ]

    # Позиция однозначно определяет, что именно будет исполнено. Раньше
    # `args.item` уходил только в котировку, а работа всегда была
    # `clause_risk`: покупатель платил за «извлечение клауз» и получал
    # агрегацию риска по готовому списку. Молчаливая подмена работы —
    # то же, что подмена ядра, только дешевле.
    workers = {
        "ailegal.clause.risk": lambda: core.clause_risk(clauses),
        "ailegal.clause.categorize": lambda: [
            core.categorize(clause) for clause in clauses
        ],
    }
    if args.item not in workers:
        supported = ", ".join(sorted(workers))
        print(
            f"ОТКАЗ: позиция {args.item!r} не имеет исполнения в этом "
            f"контуре. Доступны офлайн-позиции: {supported}. Позиции с "
            f"внешним провайдером не имитируются.",
            file=sys.stderr,
        )
        return 6

    call = PaidCall(catalog, args.rail, secret, ledger=ledger)
    try:
        outcome = call.run(
            args.item,
            _resource(args.resource),
            workers[args.item],
            allow_unverified=args.allow_unverified,
            on_duplicate=args.on_duplicate,
        )
    except UnpricedCatalog as exc:
        print(f"ОТКАЗ: {exc}", file=sys.stderr)
        return 3
    except DuplicateSettlement as exc:
        print(f"ДВОЙНОЕ СПИСАНИЕ ЗАПРЕЩЕНО: {exc}", file=sys.stderr)
        return 4
    except ProviderUnavailable as exc:
        print(f"ПРОВАЙДЕР НЕДОСТУПЕН: {exc}", file=sys.stderr)
        return 7
    except CoreUnavailable as exc:
        print(f"ЯДРО НЕДОСТУПНО: {exc}", file=sys.stderr)
        return 5

    if catalog.pricelist.unverified_rates():
        print(
            "ВНИМАНИЕ: расчёт по неподтверждённым тарифам — "
            "сумма является гипотезой, не прайсом",
            file=sys.stderr,
        )
    _emit(outcome.as_dict())
    return 0


def _cmd_ledger(args: argparse.Namespace) -> int:
    from .ledger import Ledger

    summary = Ledger(args.path).summary()
    if args.format == "json":
        _emit(summary)
    else:
        print(f"журнал: {summary['path']}")
        print(f"квитанций: {summary['receipts']}")
        print(f"всего: {summary['total']}")
        print(f"маржа: {summary['margin']}")
        for item, amount in summary["by_item"].items():
            print(f"  {item:<28} {amount}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handlers = {
        "price": _cmd_price,
        "rails": _cmd_rails,
        "quote": _cmd_quote,
        "catalog": _cmd_catalog,
        "golden": _cmd_golden,
        "run": _cmd_run,
        "ledger": _cmd_ledger,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
