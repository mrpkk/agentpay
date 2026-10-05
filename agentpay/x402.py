"""Настоящий протокол x402: разбор 402-челленджа и защита цены.

Зачем модуль: `X402Rail` в `rails.py` умеет только считать — `settle()`
делает арифметику и ни к какой сети не обращается. Денежного пути в пакете
не было: ни разбора 402, ни подписи, ни чека. Этот модуль закрывает разрыв
и содержит ту часть, которая на самом деле защищает деньги: **не дать
заплатить больше, чем согласовано заранее**.

Протокол снят живьём 2026-09-30 с провайдера agentsvc.io:
`POST /api/v1/proxy/{slug}` отдаёт HTTP 402 и присылает **обе версии
одновременно** — v1 в теле (`maxAmountRequired`, `network: "base"`), v2 в
заголовке `PAYMENT-REQUIRED` (base64, `amount`, `network: "eip155:8453"`).
Имена полей и запись сети различаются. Реализация, читающая только одну из
версий, молча ломается на другой, поэтому `parse_challenge` берёт v2 при
наличии и откатывается на v1.

Что проверяет `check()` и почему это важно:

* сумма че��ленджа не превышает ранее согласованную котировку — иначе
  сервер поднимает цену между «я узнал прайс» и «я заплатил»;
* актив совпадает с ожидаемым (USDC в Base), а не с любым ERC-20;
* сеть из белого списка, а не любая указанная сервером;
* схема известна (`exact`), чтобы не подписать незнакомую конструкцию;
* получатель платежа закреплён, если его знали заранее.

Цена, завышенная сервером, — самая естественная атака на клиента x402:
спецификация требует лишь подписать присланные параметры, а «проверить,
что это не дороже» обязан покупатель. Если этого нет, микроплатёжный
слой превращается в способ списать произвольную сумму по запросу сервера.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import secrets
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping

from .quote import SCHEME_EXACT, Quote

USDC_DECIMALS = 6
BASE_NETWORK = "eip155:8453"
USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
USDC_BASE_SEPOLIA = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"
BASE_SEPOLIA_NETWORK = "eip155:84532"

HEADER_CHALLENGE = "payment-required"
HEADER_SETTLEMENT = "payment-response"

#: Сеть в разных версиях протокола записывается по-разному, но значит одно и
#: то же. Сравнивать строки напрямую нельзя: `base` и `eip155:8453` — это
#: один и тот же Base mainnet, и платёж в одну сеть не должен проходить как
#: платёж в другую.
NETWORK_ALIASES: dict[str, str] = {
    "base": BASE_NETWORK,
    "base-mainnet": BASE_NETWORK,
    "base_mainnet": BASE_NETWORK,
    "basemainnet": BASE_NETWORK,
    "eip155:8453": BASE_NETWORK,
    "8453": BASE_NETWORK,
}


class X402Error(Exception):
    """Базовая ошибка протокола."""


class ChallengeMalformed(X402Error):
    """402-ответ не содержит обязательных полей оплаты."""


class PriceEscalation(X402Error):
    """Сервер запросил больше, чем было согласовано."""


class AssetNotAccepted(X402Error):
    """Запрошен не тот актив, который закреплён."""


class NetworkNotAccepted(X402Error):
    """Сеть платежа не входит в белый список."""


class SchemeNotSupported(X402Error):
    """Схема платежа не поддерживается."""


class PayeeMismatch(X402Error):
    """Получатель платежа не совпадает с ожидаемым."""


class ResourceMismatch(X402Error):
    """Челлендж адресован другому ресурсу, чем тот, что согласован."""


def to_atoms(amount: Decimal, decimals: int = USDC_DECIMALS) -> int:
    """Перевести сумму в атомы единицы: 0.002 USDC → 2000.

    Дробная часть квантуется вниз, а не округляется: при переводе вниз
    недовыплата невозможна в принципе, а округление вверх тихо завышало бы
    цену на единицу кванта на каждом вызове.
    """
    if decimals < 0:
        raise ValueError("decimals не может быть отрицательным")
    if amount < 0:
        raise ValueError(f"сумма отрицательна: {amount}")
    quant = Decimal(1).scaleb(-decimals)
    return int((amount / quant).to_integral_value(rounding="ROUND_FLOOR"))


def from_atoms(atoms: int, decimals: int = USDC_DECIMALS) -> Decimal:
    """Обратный перевод атомов в сумму: 2000 → 0.002."""
    if not isinstance(atoms, int) or isinstance(atoms, bool):
        raise ChallengeMalformed(f"amount должен быть целым числом, получено {atoms!r}")
    if atoms < 0:
        raise ChallengeMalformed(f"отрицательный amount: {atoms}")
    return (Decimal(atoms) * Decimal(1).scaleb(-decimals)).quantize(
        Decimal(1).scaleb(-decimals)
    )


def normalize_network(value: object) -> str:
    """Привести запись сети к канонической: `base` → `eip155:8453`.

    Известные алиасы отображаются в CAIP-10, а любая другая сеть вида
    `eip155:<chainid>` приводится к своему виду и **не отвергается**: разбор
    отвечает на вопрос «чего хочет продавец», а «согласны ли мы» решает
    `check_challenge` по белому списку. Иначе нельзя было бы показать покупателю
    «продавец просит Polygon, а мы работаем только с Base» — отказ пришлось
    бы делать по факту разбора, не разобрав само требование.
    """
    if not isinstance(value, str) or not value.strip():
        raise ChallengeMalformed(f"сеть не задана: {value!r}")
    key = value.strip().lower()
    network = NETWORK_ALIASES.get(key)
    if network is not None:
        return network
    if key.startswith("eip155:") and key[7:].isdigit():
        return key
    raise NetworkNotAccepted(f"неизвестный формат сети платежа: {value!r}")


def same_address(left: object, right: object) -> bool:
    """Сравнить адреса без учёта регистра и ведущего `0x`.

    Контрольные суммы EIP-55 меняют регистр, но не адрес, поэтому
    посимвольное сравнение с заглавными буквами отвергло бы честный адрес.
    """
    if not isinstance(left, str) or not isinstance(right, str):
        return False
    return left.strip().lower() == right.strip().lower()


@dataclass(frozen=True)
class PaymentRequirement:
    """Одноразовое требование оплаты, приведённое к общему виду."""

    scheme: str
    network: str
    amount_atoms: int
    asset: str
    pay_to: str
    max_timeout_seconds: int
    resource: str
    mime_type: str
    extra: Mapping[str, Any]
    version: int

    @property
    def amount(self) -> Decimal:
        """Сумма платежа в единицах актива."""
        return from_atoms(self.amount_atoms)


@dataclass(frozen=True)
class Challenge:
    """402-челлендж целиком: требование плюс необязательные расширения."""

    requirement: PaymentRequirement
    raw: Mapping[str, Any]

    @property
    def bazaar_input(self) -> Mapping[str, Any] | None:
        """Описание входных данных, если провайдер его прислал."""
        try:
            return self.raw["extensions"]["bazaar"]["info"]["input"]
        except (KeyError, TypeError):
            return None


@dataclass(frozen=True)
class Settlement:
    """Квитанция об оплате, разобранная из ответа 200."""

    transaction_ref: str
    network: str
    amount: Decimal
    raw: Mapping[str, Any]


def _header(headers: Mapping[str, str], name: str) -> str | None:
    """Найти заголовок без учёта регистра."""
    if headers is None:
        return None
    if name in headers:
        return headers[name]
    lowered = name.lower()
    for key, value in headers.items():
        if key.lower() == lowered:
            return value
    return None


def _decode_header_payload(value: str) -> dict[str, Any]:
    """Разобрать `PAYMENT-REQUIRED`.

    Канон — base64, но встречается и открытый JSON. Один и тот же заголовок
    не должен приводить к падению только из-за другой упаковки, поэтому
    не-UTF-8 после base64 пробуем как есть.
    """
    raw = value.strip()
    try:
        padded = raw + "=" * (-len(raw) % 4)
        decoded = base64.b64decode(padded, validate=False)
        text = decoded.decode("utf-8")
    except (binascii.Error, ValueError, UnicodeDecodeError):
        text = raw
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ChallengeMalformed(f"челлендж не разобран как JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ChallengeMalformed("челлендж должен быть объектом JSON")
    return parsed


def _pick_amount(entry: Mapping[str, Any]) -> int:
    """Достать сумму, не путая `amount` (v2) и `maxAmountRequired` (v1)."""
    for key in ("amount", "maxAmountRequired", "maxAmount"):
        if key in entry and entry[key] is not None:
            value = entry[key]
            break
    else:
        raise ChallengeMalformed("в требовании нет суммы: нет ни amount, ни maxAmountRequired")
    if isinstance(value, str):
        try:
            value = int(value, 10)
        except ValueError as exc:
            raise ChallengeMalformed(f"сумма не число: {value!r}") from exc
    return int(value)


def parse_requirement(
    entry: Mapping[str, Any],
    *,
    version: int,
    resource: str | None = None,
    mime_type: str | None = None,
) -> PaymentRequirement:
    """Собрать `PaymentRequirement` из записи `accepts` любой из версий.

    В v1 `resource` и `mimeType` лежат внутри записи, в v2 — на верхнем
    уровне челленджа, внутри `accepts[0]` их нет вовсе. И там же `resource`
    приходит объектом `{"url", "description", "mimeType"}`, а не строкой.
    Проверено на живом ответе agentsvc.io. Отсюда `resource=None` в качестве
    запасного значения: без него ни один v2-челлендж не разобрался бы.
    """
    resolved_resource = entry.get("resource") or resource
    if isinstance(resolved_resource, Mapping):
        resolved_resource = resolved_resource.get("url")
    resolved_resource = str(resolved_resource or "").strip()
    resolved_mime = mime_type
    if isinstance(resource, Mapping):
        resolved_mime = resolved_mime or resource.get("mimeType")
    for key, value in (
        ("resource", resolved_resource),
        ("payTo", entry.get("payTo")),
        ("asset", entry.get("asset")),
        ("network", entry.get("network")),
    ):
        if not value:
            raise ChallengeMalformed(f"в требовании нет обязательного поля {key!r}")
    try:
        timeout = int(entry.get("maxTimeoutSeconds") or 0)
    except (TypeError, ValueError) as exc:
        raise ChallengeMalformed("maxTimeoutSeconds должен быть целым числом") from exc
    return PaymentRequirement(
        scheme=str(entry.get("scheme") or "").strip().lower(),
        network=normalize_network(entry["network"]),
        amount_atoms=_pick_amount(entry),
        asset=str(entry["asset"]).strip(),
        pay_to=str(entry["payTo"]).strip(),
        max_timeout_seconds=timeout,
        resource=resolved_resource,
        mime_type=str(entry.get("mimeType") or resolved_mime or "application/json").strip(),
        extra=dict(entry.get("extra") or {}),
        version=version,
    )


def parse_challenge(
    headers: Mapping[str, str] | None, body: object
) -> Challenge:
    """Разобрать 402-ответ: приоритет у v2 в заголовке, затем v1 в теле.

    Порядок не формальность. Провайдеры шлют обе версии, и они иногда
    расходятся: v2 отражает текущую цену, v1 в теле может остаться от
    предыдущего развёртывания. Ставить v1 выше только потому, что он
    «подробнее», означало бы рисковать старой ценой.
    """
    header_value = _header(headers or {}, HEADER_CHALLENGE)
    if header_value:
        payload = _decode_header_payload(header_value)
        accepts = payload.get("accepts")
        if isinstance(accepts, list) and accepts and isinstance(accepts[0], dict):
            version = int(payload.get("x402Version") or 2)
            return Challenge(
                requirement=parse_requirement(
                    accepts[0],
                    version=version,
                    resource=payload.get("resource"),
                    mime_type=payload.get("mimeType"),
                ),
                raw=payload,
            )
    if isinstance(body, Mapping):
        accepts = body.get("accepts")
        if isinstance(accepts, list) and accepts and isinstance(accepts[0], dict):
            version = int(body.get("x402Version") or 1)
            return Challenge(
                requirement=parse_requirement(
                    accepts[0],
                    version=version,
                    resource=body.get("resource"),
                    mime_type=body.get("mimeType"),
                ),
                raw=dict(body),
            )
    raise ChallengeMalformed(
        "в ответе нет ни заголовка PAYMENT-REQUIRED, ни accepts в теле"
    )


def check_challenge(
    requirement: PaymentRequirement,
    quote: Quote,
    *,
    allowed_networks: tuple[str, ...] = (BASE_NETWORK,),
    expected_asset: str | None = USDC_BASE,
    expected_payee: str | None = None,
) -> Decimal:
    """Проверить челлендж против котировки. Вернёт авторизованную сумму.

    Ни одна проверка здесь не «формальность»: каждая закрывает способ
    списать деньги, которые покупатель не согласовывал. Проверки идут от
    дешёвых к дорогим, а отказ всегда бесплатен для покупателя — поэтому
    сомнительный челлендж не оплачивается, а не «оплачивается и разбирается».
    """
    if requirement.scheme != SCHEME_EXACT:
        raise SchemeNotSupported(
            f"схема {requirement.scheme!r} не поддерживается, ждём {SCHEME_EXACT!r}"
        )
    if requirement.network not in allowed_networks:
        raise NetworkNotAccepted(
            f"сеть {requirement.network!r} не входит в разрешённые {allowed_networks!r}"
        )
    if expected_asset is not None and not same_address(requirement.asset, expected_asset):
        raise AssetNotAccepted(
            f"актив {requirement.asset} не совпадает с ожидаемым {expected_asset}"
        )
    if requirement.max_timeout_seconds <= 0:
        raise ChallengeMalformed("maxTimeoutSeconds должен быть положительным")
    ceiling = quote.amount
    if quote.max_amount is not None and quote.max_amount < ceiling:
        ceiling = quote.max_amount
    offered = requirement.amount
    if offered > ceiling:
        raise PriceEscalation(
            f"сервер запросил {offered} при согласованном потолке {ceiling} "
            f"({quote.currency}); разница {offered - ceiling}"
        )
    if expected_payee is not None and not same_address(requirement.pay_to, expected_payee):
        raise PayeeMismatch(
            f"получатель {requirement.pay_to} не совпадает с ожидаемым {expected_payee}"
        )
    if quote.resource and requirement.resource:
        if not same_address(requirement.resource, quote.resource):
            raise ResourceMismatch(
                f"челлендж адресован {requirement.resource!r}, "
                f"а согласован ресурс {quote.resource!r}"
            )
    return offered


def parse_settlement(headers: Mapping[str, str] | None, body: object) -> Settlement:
    """Разобрать квитанцию об оплате из успешного ответа.

    Транзакционный хэш — единственное доказательство, что деньги ушли, и
    единственное, что можно предъявить при споре. Ответ без хэша не
    считается оплатой.
    """
    raw: dict[str, Any] = {}
    for source in (body,):
        if isinstance(source, Mapping):
            raw = dict(source)
            break
    payment = raw.get("payment")
    if not isinstance(payment, Mapping):
        payment = raw
    ref = (
        payment.get("transaction")
        or payment.get("txHash")
        or payment.get("transactionHash")
        or payment.get("hash")
        or _header(headers or {}, HEADER_SETTLEMENT)
    )
    if not isinstance(ref, str) or not ref.strip():
        raise ChallengeMalformed("в ответе об оплате нет транзакционного хэша")
    raw_amount = payment.get("amount", "0")
    try:
        amount = from_atoms(int(raw_amount)) if isinstance(raw_amount, (int, str)) else Decimal(0)
    except (TypeError, ValueError, InvalidOperation, ChallengeMalformed):
        amount = Decimal(0)
    network = payment.get("network") or raw.get("network") or BASE_NETWORK
    return Settlement(
        transaction_ref=ref.strip(),
        network=normalize_network(network),
        amount=amount,
        raw=raw,
    )


# ──────────────────────────── Сборка подписи `exact` ────────────────────────────
#
# ВАЖНО, ПОЧЕМУ ЗДЕСЬ НЕТ КРИПТОГРАФИИ И НЕТ ЗАВИСИМОСТЕЙ
#
# `agentpay` не держит приватный ключ и не подписывает. Подпись делает
# кошелёк владельца, проверяет контракт токена, а агент лишь собирает
# структуру, которую нужно подписать, и проверяет её до отправки.
# Это не экономия, а требование денежного пути: библиотека, которая
# переживает запрос, не должна иметь возможности им распоряжаться.
#
# По той же причине домен EIP-712 не угадывается по имени токена, а берётся
# из проверенной таблицы: опечатка в `name` или `version` даёт подпись,
# которую контракт отвергнет, и деньги сгорят в газе.


class TokenDomainUnknown(X402Error):
    """Домен EIP-712 токена не проверен — строить подпись нельзя.

    Лучше отказ на этапе сборки, чем подпись, которую контракт не примет.
    """


class AuthorizationMismatch(X402Error):
    """Собираемая авторизация не соответствует полученному челленджу."""


@dataclass(frozen=True)
class EIP712Domain:
    """Домен подписи EIP-712: часть типизированных данных, не подпись."""

    name: str
    version: str
    verifying_contract: str
    chain_id: int


# Проверено 2026-09-30 вызовами eth_call к прокси USDC в сети Base:
#   name()     -> "USD Coin"   (0x06fdde03)
#   version()  -> "2"          (0x54fd4d50)
#   decimals() -> 6            (0x313ce567)
#   chainId 8453 — определение сети Base; самого `chainId()` у токена нет.
# Сырые ответы лежат в tests/fixtures/agentsvc_402.json -> _chain_evidence,
# и тест сверяет таблицу с этими ответами, а не только с собой.
VERIFIED_DOMAINS: dict[tuple[str, str], EIP712Domain] = {
    (BASE_NETWORK, USDC_BASE.lower()): EIP712Domain(
        name="USD Coin",
        version="2",
        verifying_contract=USDC_BASE,
        chain_id=8453,
    ),
    # Base Sepolia. Имя здесь «USDC», а на mainnet «USD Coin» — один и тот же
    # токен в разных сетях. Подтверждено 2026-10-03 проверкой подписи на
    # настоящем фасилитаре: с именем «USD Coin» контракт отвечает
    # «invalid signature», с именем «USDC» — «transfer amount exceeds
    # balance», то есть подпись принята.
    (BASE_SEPOLIA_NETWORK, USDC_BASE_SEPOLIA.lower()): EIP712Domain(
        name="USDC",
        version="2",
        verifying_contract=USDC_BASE_SEPOLIA,
        chain_id=84532,
    ),
}

MAX_ALLOWED_SIGNATURE_WINDOW_SECONDS = 3600
DEFAULT_SIGNATURE_WINDOW_SECONDS = 300


def eip712_domain(requirement: PaymentRequirement) -> EIP712Domain:
    """Домен подписи для требования; отказ, если токен не проверен."""
    key = (requirement.network, requirement.asset.strip().lower())
    domain = VERIFIED_DOMAINS.get(key)
    if domain is None:
        raise TokenDomainUnknown(
            f"домен EIP-712 для актива {requirement.asset} в сети "
            f"{requirement.network} не проверен; подпись строить нельзя"
        )
    return domain


def new_payment_nonce() -> str:
    """Свежий 32-байтный nonce в виде 0x-строки, как требует EIP-3009."""
    return "0x" + secrets.token_bytes(32).hex()


def build_exact_authorization(
    requirement: PaymentRequirement,
    from_address: str,
    *,
    nonce: str | None = None,
    now: int | None = None,
    window_seconds: int = DEFAULT_SIGNATURE_WINDOW_SECONDS,
) -> dict[str, str]:
    """Собрать `authorization` для подписи кошельком.

    Поле `value` берётся из самого челленджа и не является параметром: если
    дать вызывающему возможность его задать, завышение цены станет
    тривиальной опечаткой. `validBefore` ограничено сверху, чтобы подпись
    нельзя было подписать «на будущее» и месяцами держать в запасе.
    """
    moment = int(time.time()) if now is None else int(now)
    if not 0 < window_seconds <= MAX_ALLOWED_SIGNATURE_WINDOW_SECONDS:
        raise AuthorizationMismatch(
            f"окно подписи должно быть в пределах "
            f"1..{MAX_ALLOWED_SIGNATURE_WINDOW_SECONDS} с, получено {window_seconds}"
        )
    if requirement.amount_atoms <= 0:
        raise AuthorizationMismatch("в требовании неположительная сумма")
    if not re.fullmatch(r"0x[0-9a-fA-F]{64}", nonce or ""):
        if nonce is not None:
            raise AuthorizationMismatch("nonce должен быть 32-байтной 0x-строкой")
        nonce = new_payment_nonce()
    return {
        "from": from_address,
        "to": requirement.pay_to,
        "value": str(requirement.amount_atoms),
        "validAfter": "0",
        "validBefore": str(moment + window_seconds),
        "nonce": nonce,
    }


def build_exact_payload(
    requirement: PaymentRequirement,
    signature: str,
    authorization: Mapping[str, str],
) -> dict[str, Any]:
    """Собрать тело `X-PAYMENT` для схемы `exact`."""
    if not re.fullmatch(r"0x[0-9a-fA-F]{130}", signature or ""):
        raise AuthorizationMismatch("подпись должна быть 65-байтной 0x-строкой")
    return {
        "x402Version": requirement.version,
        "scheme": "exact",
        "network": requirement.network,
        "payload": {
            "signature": signature,
            "authorization": dict(authorization),
        },
    }


def check_exact_payload(
    requirement: PaymentRequirement,
    payload: Mapping[str, Any],
    *,
    expected_payer: str | None = None,
) -> dict[str, str]:
    """Проверить собранную подпись **до** отправки и вернуть `authorization`.

    Подпись локально не проверяется и это намеренно: единственный
    независимый проверяющий — контракт токена, и подделка подписи просто
    не пройдёт расчёт. Зато за один проход ловятся ошибки, которые иначе
    оплачиваются газом: не тот получатель, не та сумма, просроченное окно.
    """
    inner = payload.get("payload")
    if not isinstance(inner, Mapping):
        raise AuthorizationMismatch("в платеже нет блока payload")
    if str(payload.get("scheme") or "") != "exact":
        raise AuthorizationMismatch("схема платежа не exact")
    if str(payload.get("network") or "") != requirement.network:
        raise AuthorizationMismatch("сеть платежа не совпадает с челленджем")
    authorization = inner.get("authorization")
    if not isinstance(authorization, Mapping):
        raise AuthorizationMismatch("в платеже нет авторизации")
    auth = {str(k): str(v) for k, v in authorization.items()}
    for field in ("from", "to", "value", "validAfter", "validBefore", "nonce"):
        if not auth.get(field):
            raise AuthorizationMismatch(f"в авторизации нет поля {field!r}")
    if not same_address(auth["to"], requirement.pay_to):
        raise AuthorizationMismatch(
            f"авторизация платит {auth['to']}, а челлендж требует {requirement.pay_to}"
        )
    if auth["value"] != str(requirement.amount_atoms):
        raise AuthorizationMismatch(
            f"авторизация на {auth['value']} атомов, челлендж требует "
            f"{requirement.amount_atoms}"
        )
    if expected_payer is not None and not same_address(auth["from"], expected_payer):
        raise AuthorizationMismatch(
            f"платит {auth['from']}, а ожидался {expected_payer}"
        )
    try:
        valid_before = int(auth["validBefore"])
        valid_after = int(auth["validAfter"])
    except ValueError as exc:
        raise AuthorizationMismatch("validAfter/validBefore должны быть числами") from exc
    if valid_after < 0:
        raise AuthorizationMismatch("validAfter отрицателен")
    if not re.fullmatch(r"0x[0-9a-fA-F]{64}", auth["nonce"]):
        raise AuthorizationMismatch("nonce должен быть 32-байтной 0x-строкой")
    if not re.fullmatch(r"0x[0-9a-fA-F]{130}", str(inner.get("signature") or "")):
        raise AuthorizationMismatch("подпись должна быть 65-байтной 0x-строкой")
    now = int(time.time())
    if valid_before <= now:
        raise AuthorizationMismatch(
            f"подпись просрочена: validBefore={valid_before}, сейчас {now}"
        )
    if valid_before - now > MAX_ALLOWED_SIGNATURE_WINDOW_SECONDS:
        raise AuthorizationMismatch(
            f"окно подписи {valid_before - now} с превышает предел "
            f"{MAX_ALLOWED_SIGNATURE_WINDOW_SECONDS} с"
        )
    return auth
