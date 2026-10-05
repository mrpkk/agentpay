"""Тесты реального протокола x402.

Проверяется не «умеет ли код платить», а «не даст ли он заплатить больше
согласованного». Фикстуры сняты с живого провайдера agentsvc.io 2026-09-30,
поэтому тесты ломаются, если протокол или наши допущения о нём расходятся.

Сеть не используется: проверяется чистое решение на реальных ответах.
"""

from __future__ import annotations

import base64
import json
import re
import sys
from decimal import Decimal
from pathlib import Path

from dataclasses import replace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agentpay.x402 as x402  # noqa: E402
from agentpay.quote import SCHEME_EXACT, new_nonce, sign_quote  # noqa: E402

FIXTURE = json.loads(
    (Path(__file__).resolve().parent / "fixtures" / "agentsvc_402.json").read_text(
        encoding="utf-8"
    )
)
REAL_V2_HEADER = FIXTURE["header_payment_required"]
REAL_V1_BODY = FIXTURE["body"]

WALLET = "0x4444444444444444444444444444444444444444"
REAL_PAYEE = REAL_V1_BODY["accepts"][0]["payTo"]
REAL_RESOURCE = REAL_V1_BODY["accepts"][0]["resource"]
# Живой челлендж agentsvc.io как есть: v2 из заголовка, v1 из тела.
CHALLENGE = x402.parse_challenge({"payment-required": REAL_V2_HEADER}, REAL_V1_BODY)


def _quote(amount: str = "0.002", **kwargs):
    return sign_quote(
        quote_id="q-1",
        item="barcode",
        amount=Decimal(amount),
        currency="USDC",
        rail="x402",
        scheme=SCHEME_EXACT,
        resource=kwargs.pop("resource", REAL_RESOURCE),
        secret=b"x402-test-secret",
        **kwargs,
    )


def _v2_headers(**extra):
    return {"payment-required": REAL_V2_HEADER, **extra}


class TestRealChallenge:
    """Разбор настоящего ответа провайдера."""

    def test_v2_header_wins_and_parses(self):
        challenge = x402.parse_challenge(_v2_headers(), REAL_V1_BODY)
        assert challenge.requirement.version == 2
        assert challenge.requirement.scheme == "exact"
        assert challenge.requirement.network == "eip155:8453"
        assert challenge.requirement.amount == Decimal("0.002000")
        assert challenge.requirement.amount_atoms == 2000
        assert challenge.requirement.max_timeout_seconds == 300
        assert x402.same_address(challenge.requirement.asset, x402.USDC_BASE)
        assert x402.same_address(challenge.requirement.pay_to, REAL_PAYEE)

    def test_v1_body_is_readable_when_header_absent(self):
        challenge = x402.parse_challenge({}, REAL_V1_BODY)
        assert challenge.requirement.version == 1
        assert challenge.requirement.network == "eip155:8453"
        assert challenge.requirement.amount == Decimal("0.002000")

    def test_header_beats_body_when_prices_disagree(self):
        """v1 в теле может остаться от старого развёртывания.

        Ставить v1 выше только потому, что он подробнее, означало бы
        рисковать старой ценой. Поэтому приоритет у заголовка.
        """
        stale = json.loads(json.dumps(REAL_V1_BODY))
        stale["accepts"][0]["maxAmountRequired"] = "1"
        challenge = x402.parse_challenge(_v2_headers(), stale)
        assert challenge.requirement.amount_atoms == 2000

    def test_plain_json_header_is_accepted(self):
        payload = json.dumps(
            {"x402Version": 2, "accepts": REAL_V1_BODY["accepts"]}
        )
        challenge = x402.parse_challenge({"Payment-Required": payload}, None)
        assert challenge.requirement.amount_atoms == 2000

    def test_header_name_is_case_insensitive(self):
        challenge = x402.parse_challenge({"PAYMENT-REQUIRED": REAL_V2_HEADER}, None)
        assert challenge.requirement.version == 2

    def test_bazaar_input_is_exposed(self):
        challenge = x402.parse_challenge(_v2_headers(), None)
        bazaar = challenge.bazaar_input
        assert bazaar is not None
        assert bazaar["method"] == "POST"

    def test_missing_challenge_is_rejected(self):
        with pytest.raises(x402.ChallengeMalformed):
            x402.parse_challenge({}, {"error": "nope"})


class TestAtoms:
    def test_roundtrip(self):
        assert x402.to_atoms(Decimal("0.002")) == 2000
        assert x402.from_atoms(2000) == Decimal("0.002000")

    def test_quantizes_down_never_up(self):
        """Округление вверх тихо завышало бы цену на квант каждый вызов."""
        assert x402.to_atoms(Decimal("0.0020009")) == 2000
        assert x402.to_atoms(Decimal("0.0019999")) == 1999

    def test_rejects_negative_and_bool(self):
        with pytest.raises(ValueError):
            x402.to_atoms(Decimal("-0.001"))
        with pytest.raises(x402.ChallengeMalformed):
            x402.from_atoms(True)


class TestNetworkAliases:
    def test_base_and_caip10_are_the_same_network(self):
        assert x402.normalize_network("base") == x402.BASE_NETWORK
        assert x402.normalize_network("eip155:8453") == x402.BASE_NETWORK
        assert x402.normalize_network("BASE") == x402.BASE_NETWORK

    def test_other_network_is_parsed_but_not_confused_with_base(self):
        """Разбор показывает, чего хочет продавец; решение — за `check`.

        Если отвергать чужую сеть на этапе разбора, покупателю нечем
        объяснить, что именно предлагает продавец.
        """
        assert x402.normalize_network("eip155:1") == "eip155:1"
        assert x402.normalize_network("eip155:1") != x402.BASE_NETWORK

    def test_garbage_network_is_rejected(self):
        with pytest.raises(x402.NetworkNotAccepted):
            x402.normalize_network("polygon-but-not-really")


class TestCheckAgainstQuote:
    """Деньги: клиент не должен платить больше согласованного."""

    def test_real_challenge_passes_at_agreed_price(self):
        challenge = x402.parse_challenge(_v2_headers(), None)
        paid = x402.check_challenge(challenge.requirement, _quote("0.002"))
        assert paid == Decimal("0.002000")

    def test_price_escalation_is_refused(self):
        """Подъём цены между прайсом и оплатой — главная атака на x402."""
        challenge = x402.parse_challenge(_v2_headers(), None)
        with pytest.raises(x402.PriceEscalation) as exc:
            x402.check_challenge(challenge.requirement, _quote("0.001"))
        assert "0.002" in str(exc.value)

    def test_ceiling_ceases_when_below_amount(self):
        challenge = x402.parse_challenge(_v2_headers(), None)
        with pytest.raises(x402.PriceEscalation):
            x402.check_challenge(
                challenge.requirement,
                _quote("0.002", max_amount=Decimal("0.0015")),
            )

    def test_cheaper_challenge_is_accepted(self):
        """Продавец не может сделать дешевле только на словах — но и не
        должен: меньшая цена не опасна, авторизация сверху."""
        challenge = x402.parse_challenge(_v2_headers(), None)
        paid = x402.check_challenge(challenge.requirement, _quote("0.010"))
        assert paid == Decimal("0.002000")

    def test_unknown_scheme_is_refused(self):
        payload = json.loads(base64.b64decode(REAL_V2_HEADER + "=="))
        payload["accepts"][0]["scheme"] = "trust-me"
        challenge = x402.parse_challenge(
            {"payment-required": json.dumps(payload)}, None
        )
        with pytest.raises(x402.SchemeNotSupported):
            x402.check_challenge(challenge.requirement, _quote())

    def test_foreign_asset_is_refused(self):
        payload = json.loads(base64.b64decode(REAL_V2_HEADER + "=="))
        payload["accepts"][0]["asset"] = "0x1111111111111111111111111111111111111111"
        challenge = x402.parse_challenge(
            {"payment-required": json.dumps(payload)}, None
        )
        with pytest.raises(x402.AssetNotAccepted):
            x402.check_challenge(challenge.requirement, _quote())

    def test_foreign_network_is_refused_even_if_under_price(self):
        payload = json.loads(base64.b64decode(REAL_V2_HEADER + "=="))
        payload["accepts"][0]["network"] = "eip155:137"
        challenge = x402.parse_challenge(
            {"payment-required": json.dumps(payload)}, None
        )
        with pytest.raises(x402.NetworkNotAccepted):
            x402.check_challenge(challenge.requirement, _quote())

    def test_addresses_compare_without_checksum_case(self):
        assert x402.same_address(
            "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            "0x833589fcd6edb6e08f4c7c32d4f71b54bdA02913",
        )

    def test_payee_is_enforced_when_pinned(self):
        challenge = x402.parse_challenge(_v2_headers(), None)
        x402.check_challenge(
            challenge.requirement, _quote(), expected_payee=REAL_PAYEE
        )
        with pytest.raises(x402.PayeeMismatch):
            x402.check_challenge(
                challenge.requirement,
                _quote(),
                expected_payee="0x0000000000000000000000000000000000000000",
            )

    def test_resource_binding_prevents_redirect(self):
        """Оплата авторизует ровно этот вызов этого ресурса, а не «что угодно
        у продавца». Смена адресата — это подмена предмета оплаты."""
        challenge = x402.parse_challenge(_v2_headers(), None)
        with pytest.raises(x402.ResourceMismatch):
            x402.check_challenge(
                challenge.requirement,
                _quote(resource="https://agentsvc.io/api/v1/proxy/other"),
            )

    def test_zero_timeout_is_rejected(self):
        payload = json.loads(base64.b64decode(REAL_V2_HEADER + "=="))
        payload["accepts"][0]["maxTimeoutSeconds"] = 0
        challenge = x402.parse_challenge(
            {"payment-required": json.dumps(payload)}, None
        )
        with pytest.raises(x402.ChallengeMalformed):
            x402.check_challenge(challenge.requirement, _quote())


class TestSettlement:
    def test_transaction_hash_is_extracted(self):
        receipt = x402.parse_settlement(
            {},
            {"payment": {"transaction": "0xabc123", "amount": 2000, "network": "base"}},
        )
        assert receipt.transaction_ref == "0xabc123"
        assert receipt.amount == Decimal("0.002000")
        assert receipt.network == x402.BASE_NETWORK

    def test_response_without_hash_is_not_a_payment(self):
        """Квитанция без хэша не доказывает оплату."""
        with pytest.raises(x402.ChallengeMalformed):
            x402.parse_settlement({}, {"data": {"ok": True}})


class TestExactSigning:
    """Сборка подписи `exact`. Ключей в библиотеке нет и не должно быть."""

    def test_domain_matches_chain_evidence_not_just_itself(self):
        """Домен сверяется с реальными ответами eth_call, а не с константой."""
        ev = FIXTURE["_chain_evidence"]
        domain = x402.eip712_domain(CHALLENGE.requirement)
        assert domain.name == ev["name()"]["decoded"] == "USD Coin"
        assert domain.version == ev["version()"]["decoded"] == "2"
        assert domain.chain_id == ev["chain_id"]["value"] == 8453
        assert domain.verifying_contract == ev["verifying_contract"]["address"]
        assert x402.USDC_DECIMALS == ev["decimals()"]["decoded"] == 6

    def test_unknown_token_domain_is_refused(self):
        """Непроверенный домен: лучше отказ, чем подпись, которую отвергнут."""
        forged = replace(
            CHALLENGE.requirement, asset="0x1111111111111111111111111111111111111111"
        )
        with pytest.raises(x402.TokenDomainUnknown):
            x402.eip712_domain(forged)

    def test_usdc_on_another_chain_is_refused(self):
        """USDC в сети, для которой домен не сверяли, — не то же, что USDC в Base."""
        elsewhere = replace(CHALLENGE.requirement, network="eip155:1")
        with pytest.raises(x402.TokenDomainUnknown):
            x402.eip712_domain(elsewhere)

    def test_authorization_takes_value_from_challenge_not_caller(self):
        """Цена берётся из челленджа: вызывающий не может её задать."""
        auth = x402.build_exact_authorization(
            CHALLENGE.requirement, WALLET, nonce="0x" + "ab" * 32, now=1_000_000
        )
        assert auth["to"].lower() == REAL_PAYEE.lower()
        assert auth["value"] == str(CHALLENGE.requirement.amount_atoms) == "2000"
        assert auth["from"] == WALLET
        assert auth["validAfter"] == "0"

    def test_authorization_window_is_bounded(self):
        """Подпись нельзя подписать «на месяц вперёд»."""
        for window in (0, -5, x402.MAX_ALLOWED_SIGNATURE_WINDOW_SECONDS + 1):
            with pytest.raises(x402.AuthorizationMismatch):
                x402.build_exact_authorization(
                    CHALLENGE.requirement, WALLET, now=1_000_000, window_seconds=window
                )

    def test_generated_nonce_is_32_bytes_and_fresh(self):
        a = x402.build_exact_authorization(CHALLENGE.requirement, WALLET, now=1_000_000)
        b = x402.build_exact_authorization(CHALLENGE.requirement, WALLET, now=1_000_000)
        assert re.fullmatch(r"0x[0-9a-f]{64}", a["nonce"])
        assert a["nonce"] != b["nonce"]

    def test_full_payload_has_x402_exact_shape(self):
        auth = x402.build_exact_authorization(
            CHALLENGE.requirement, WALLET, nonce="0x" + "cd" * 32
        )
        payload = x402.build_exact_payload(
            CHALLENGE.requirement, "0x" + "11" * 65, auth
        )
        assert payload["x402Version"] == 2
        assert payload["scheme"] == "exact"
        assert payload["network"] == "eip155:8453"
        assert payload["payload"]["authorization"] == auth
        # тело должно переживать сериализацию без потерь — это уйдёт в HTTP-заголовок
        assert json.loads(json.dumps(payload)) == payload

    def test_malformed_signature_is_refused(self):
        auth = x402.build_exact_authorization(
            CHALLENGE.requirement, WALLET, nonce="0x" + "cd" * 32
        )
        for bad in ("", "0xdead", "0x" + "11" * 64, "not hex"):
            with pytest.raises(x402.AuthorizationMismatch):
                x402.build_exact_payload(CHALLENGE.requirement, bad, auth)

    def test_payload_built_for_another_challenge_is_caught_before_sending(self):
        """Самая дорогая ошибка: подписали не то. Ловим до отправки."""
        auth = x402.build_exact_authorization(
            CHALLENGE.requirement, WALLET, nonce="0x" + "cd" * 32
        )
        payload = x402.build_exact_payload(
            CHALLENGE.requirement, "0x" + "11" * 65, auth
        )
        x402.check_exact_payload(CHALLENGE.requirement, payload, expected_payer=WALLET)
        other = replace(
            CHALLENGE.requirement, pay_to="0x" + "22" * 20, amount_atoms=999_999
        )
        with pytest.raises(x402.AuthorizationMismatch):
            x402.check_exact_payload(other, payload, expected_payer=WALLET)

    def test_payee_change_alone_is_caught(self):
        """Меняется ТОЛЬКО получатель — поймать может лишь проверка получателя."""
        auth = x402.build_exact_authorization(
            CHALLENGE.requirement, WALLET, nonce="0x" + "cd" * 32
        )
        payload = x402.build_exact_payload(
            CHALLENGE.requirement, "0x" + "11" * 65, auth
        )
        redirected = replace(CHALLENGE.requirement, pay_to="0x" + "22" * 20)
        assert redirected.amount_atoms == CHALLENGE.requirement.amount_atoms
        with pytest.raises(x402.AuthorizationMismatch, match="челлендж требует"):
            x402.check_exact_payload(redirected, payload, expected_payer=WALLET)

    def test_amount_change_alone_is_caught(self):
        """Меняется ТОЛЬКО сумма — поймать может лишь сверка суммы."""
        auth = x402.build_exact_authorization(
            CHALLENGE.requirement, WALLET, nonce="0x" + "cd" * 32
        )
        payload = x402.build_exact_payload(
            CHALLENGE.requirement, "0x" + "11" * 65, auth
        )
        inflated = replace(CHALLENGE.requirement, amount_atoms=2001)
        assert inflated.pay_to == CHALLENGE.requirement.pay_to
        with pytest.raises(x402.AuthorizationMismatch, match="атомов"):
            x402.check_exact_payload(inflated, payload, expected_payer=WALLET)

    def test_payer_mismatch_is_refused(self):
        auth = x402.build_exact_authorization(
            CHALLENGE.requirement, WALLET, nonce="0x" + "cd" * 32
        )
        payload = x402.build_exact_payload(
            CHALLENGE.requirement, "0x" + "11" * 65, auth
        )
        with pytest.raises(x402.AuthorizationMismatch):
            x402.check_exact_payload(
                CHALLENGE.requirement, payload, expected_payer="0x" + "33" * 20
            )

    def test_expired_signature_is_refused(self):
        past = x402.build_exact_authorization(
            CHALLENGE.requirement, WALLET, nonce="0x" + "cd" * 32, now=1
        )
        payload = x402.build_exact_payload(
            CHALLENGE.requirement, "0x" + "11" * 65, past
        )
        with pytest.raises(x402.AuthorizationMismatch, match="просрочена"):
            x402.check_exact_payload(CHALLENGE.requirement, payload)

    def test_short_lived_payload_passes(self):
        auth = x402.build_exact_authorization(
            CHALLENGE.requirement, WALLET, nonce="0x" + "cd" * 32
        )
        payload = x402.build_exact_payload(
            CHALLENGE.requirement, "0x" + "11" * 65, auth
        )
        assert x402.check_exact_payload(
            CHALLENGE.requirement, payload, expected_payer=WALLET
        ) == auth
