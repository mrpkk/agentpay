"""Регрессионные тесты agentpay.

Приоритет — негативные случаи: подделка подписи, replay, истечение срока,
превышение потолка и попытка навязать чужую цену. Позитивный путь проверяет
арифметику.
"""

from __future__ import annotations

import json
import threading
from decimal import Decimal

import pytest

from agentpay import (
    SCHEME_EXACT,
    SCHEME_UPTO,
    CostBreakdown,
    MandateRail,
    MemoryNonceStore,
    MockRail,
    NonceStore,
    PriceItem,
    PriceList,
    Quote,
    RateCard,
    Verdict,
    VerdictReason,
    X402Rail,
    available,
    check,
    consume,
    get,
    settle_amount,
    sign_quote,
)

SECRET = b"unit-test-secret"
OTHER_SECRET = b"attacker-secret"


def make_rates(**overrides: Decimal) -> RateCard:
    base = {
        "usd_per_million_tokens": Decimal("1.00"),
        "usd_per_cpu_hour": Decimal("0.60"),
        "usd_per_megabyte_out": Decimal("0.10"),
        "usd_per_storage_write": Decimal("0.005"),
        "usd_per_external_call": Decimal("0.002"),
        "verified_on": "2026-09-29",
        "source": "test-fixture",
    }
    base.update(overrides)
    return RateCard(**base)


def make_pricelist(**kwargs: object) -> PriceList:
    items = (
        PriceItem(
            name="clause.extract",
            usage=CostBreakdown(
                tokens=Decimal("1_000_000"),
                cpu_seconds=Decimal("3600"),
                bytes_out=Decimal("1_000_000"),
                storage_writes=Decimal(10),
                external_calls=Decimal(20),
            ),
        ),
        PriceItem(
            name="free.ping",
            usage=CostBreakdown(cpu_seconds=Decimal("0")),
        ),
    )
    return PriceList(
        currency=kwargs.pop("currency", "USD"),
        items=items,
        rates=kwargs.pop("rates", make_rates()),
        **kwargs,
    )


def make_quote(pricelist: PriceList, **kwargs: object) -> Quote:
    item = kwargs.pop("item", "clause.extract")
    params = {
        "quote_id": "q-1",
        "item": item,
        "amount": pricelist.price_of(item),
        "currency": pricelist.currency,
        "rail": "mock",
        "resource": "urn:agentpay:seller:abc",
        "secret": SECRET,
    }
    params.update(kwargs)
    return sign_quote(**params)  # type: ignore[arg-type]


class TestPricing:
    def test_cost_is_sum_of_components(self) -> None:
        pricelist = make_pricelist()
        # 1M токенов * $1.00 = 1.00
        # 1 cpu-час * $0.60  = 0.60
        # 1MB исходящих * $0.10 = 0.10
        # 10 записей * $0.005 = 0.05
        # 20 вызовов * $0.002 = 0.04
        assert pricelist.cost_of("clause.extract") == Decimal("1.79")

    def test_price_applies_target_margin_and_rounds_up(self) -> None:
        pricelist = make_pricelist()
        # 1.79 / (1 - 0.60) = 4.475 -> ceil до цента = 4.48
        assert pricelist.price_of("clause.extract") == Decimal("4.48")
        assert pricelist.margin_of("clause.extract") >= pricelist.target_margin

    def test_dividing_by_margin_instead_of_complement_is_a_bug(self) -> None:
        """Страховка от возврата формулы с/m: при m=0.60 такая цена дала бы
        маржу 0.40 вместо 0.60."""
        pricelist = make_pricelist()
        cost = pricelist.cost_of("clause.extract")
        wrong = (cost / pricelist.target_margin).quantize(Decimal("0.01"))
        assert pricelist.margin_of("clause.extract") >= pricelist.target_margin
        assert pricelist.price_of("clause.extract") > wrong

    def test_rounding_never_dips_below_margin(self) -> None:
        pricelist = make_pricelist(
            rates=make_rates(usd_per_million_tokens=Decimal("0.0000001"))
        )
        price = pricelist.price_of("clause.extract")
        assert price > 0
        assert pricelist.margin_of("clause.extract") >= pricelist.target_margin

    def test_zero_cost_item_is_free(self) -> None:
        pricelist = make_pricelist()
        assert pricelist.price_of("free.ping") == Decimal(0)
        assert pricelist.margin_of("free.ping") == Decimal(0)

    def test_unverified_rates_are_flagged(self) -> None:
        rates = make_rates(verified_on="", source="")
        pricelist = make_pricelist(rates=rates)
        assert pricelist.unverified_rates() is True
        assert make_pricelist().unverified_rates() is False

    def test_negative_usage_rejected(self) -> None:
        with pytest.raises(ValueError, match="отрицателен"):
            CostBreakdown(tokens=Decimal(-1))

    def test_invalid_margin_rejected(self) -> None:
        with pytest.raises(ValueError, match="target_margin"):
            make_pricelist(target_margin=Decimal(0))

    def test_duplicate_item_rejected(self) -> None:
        with pytest.raises(ValueError, match="дубликат"):
            PriceList(
                currency="USD",
                items=(
                    PriceItem(name="a", usage=CostBreakdown()),
                    PriceItem(name="a", usage=CostBreakdown()),
                ),
                rates=make_rates(),
            )

    def test_unknown_item_raises(self) -> None:
        with pytest.raises(KeyError):
            make_pricelist().price_of("нет-такого")


class TestQuote:
    def test_signature_roundtrip(self) -> None:
        pricelist = make_pricelist()
        quote = make_quote(pricelist)
        assert quote.signature_valid(SECRET)
        assert not quote.signature_valid(OTHER_SECRET)

    def test_tampered_amount_invalidates_signature(self) -> None:
        from dataclasses import replace

        pricelist = make_pricelist()
        quote = make_quote(pricelist)
        tampered = replace(quote, amount=Decimal("0.01"))
        assert not tampered.signature_valid(SECRET)

    def test_tampered_resource_invalidates_signature(self) -> None:
        from dataclasses import replace

        pricelist = make_pricelist()
        quote = make_quote(pricelist)
        tampered = replace(quote, resource="urn:agentpay:attacker:xyz")
        assert not tampered.signature_valid(SECRET)

    def test_expiry_checked(self) -> None:
        pricelist = make_pricelist()
        quote = make_quote(pricelist, ttl_seconds=10, now=1000)
        assert not quote.is_expired(1005)
        assert quote.is_expired(1010)
        assert quote.is_expired(1011)

    def test_nonce_is_unique(self) -> None:
        pricelist = make_pricelist()
        nonces = {make_quote(pricelist).nonce for _ in range(50)}
        assert len(nonces) == 50

    def test_upto_requires_ceiling(self) -> None:
        with pytest.raises(ValueError, match="max_amount"):
            make_quote(make_pricelist(), scheme=SCHEME_UPTO)

    def test_ceiling_below_amount_rejected(self) -> None:
        with pytest.raises(ValueError, match="max_amount меньше"):
            make_quote(
                make_pricelist(), scheme=SCHEME_UPTO, max_amount=Decimal("0.01")
            )

    def test_quote_is_json_serialisable(self) -> None:
        quote = make_quote(make_pricelist())
        document = json.loads(quote._payload())
        assert document["amount"] == str(quote.amount)
        assert "signature" not in document

    def test_empty_resource_rejected(self) -> None:
        with pytest.raises(ValueError, match="resource обязателен"):
            make_quote(make_pricelist(), resource="   ")

    def test_digest_deterministic_for_identical_fields(self) -> None:
        from dataclasses import replace

        quote = make_quote(make_pricelist())
        same_fields = replace(quote, signature="другая-подпись")
        assert same_fields.digest() == quote.digest()
        assert replace(quote, item="другое").digest() != quote.digest()

    def test_non_positive_ttl_rejected(self) -> None:
        with pytest.raises(ValueError, match="ttl_seconds"):
            make_quote(make_pricelist(), ttl_seconds=0)


class TestCheck:
    def test_valid_quote_accepted(self) -> None:
        pricelist = make_pricelist()
        quote = make_quote(pricelist)
        verdict, reason, _ = check(quote, pricelist, SECRET)
        assert verdict is Verdict.ACCEPT
        assert reason is VerdictReason.OK

    def test_unsigned_quote_rejected(self) -> None:
        from dataclasses import replace

        pricelist = make_pricelist()
        quote = replace(make_quote(pricelist), signature="")
        verdict, reason, _ = check(quote, pricelist, SECRET)
        assert verdict is Verdict.REJECT
        assert reason is VerdictReason.QUOTE_NOT_SIGNED

    def test_forged_signature_rejected(self) -> None:
        from dataclasses import replace

        pricelist = make_pricelist()
        quote = make_quote(pricelist, secret=OTHER_SECRET)
        verdict, reason, _ = check(quote, pricelist, SECRET)
        assert verdict is Verdict.REJECT
        assert reason is VerdictReason.SIGNATURE_INVALID

    def test_expired_quote_rejected(self) -> None:
        pricelist = make_pricelist()
        quote = make_quote(pricelist, ttl_seconds=10, now=1000)
        verdict, reason, _ = check(quote, pricelist, SECRET, now=1100)
        assert verdict is Verdict.REJECT
        assert reason is VerdictReason.QUOTE_EXPIRED

    def test_replay_rejected(self) -> None:
        pricelist = make_pricelist()
        quote = make_quote(pricelist)
        nonces = MemoryNonceStore()
        verdict, _reason, _ = check(quote, pricelist, SECRET, nonces=nonces)
        assert verdict is Verdict.ACCEPT
        consume(nonces, quote)
        verdict, reason, _ = check(quote, pricelist, SECRET, nonces=nonces)
        assert verdict is Verdict.REJECT
        assert reason is VerdictReason.REPLAY_DETECTED

    def test_expired_nonce_is_reusable(self) -> None:
        nonces = MemoryNonceStore()
        nonces.remember("n-1", expires_at=1)
        assert nonces.seen("n-1") is False

    def test_overcharge_rejected(self) -> None:
        pricelist = make_pricelist()
        quote = make_quote(pricelist, amount=Decimal("99"))
        verdict, reason, _ = check(quote, pricelist, SECRET)
        assert verdict is Verdict.REJECT
        assert reason is VerdictReason.AMOUNT_MISMATCH

    def test_undercharge_goes_to_review_not_accept(self) -> None:
        pricelist = make_pricelist()
        quote = make_quote(pricelist, amount=Decimal("0.01"))
        verdict, reason, _ = check(quote, pricelist, SECRET)
        assert verdict is Verdict.REVIEW
        assert reason is VerdictReason.AMOUNT_MISMATCH

    def test_ceiling_above_catalog_rejected(self) -> None:
        pricelist = make_pricelist()
        quote = make_quote(
            pricelist,
            scheme=SCHEME_UPTO,
            max_amount=Decimal("1000"),
        )
        verdict, reason, _ = check(quote, pricelist, SECRET)
        assert verdict is Verdict.REJECT
        assert reason is VerdictReason.CEILING_EXCEEDED

    def test_unknown_item_rejected(self) -> None:
        pricelist = make_pricelist()
        quote = sign_quote(
            quote_id="q-x",
            item="выдуманный-продукт",
            amount=Decimal("1.00"),
            currency="USD",
            rail="mock",
            resource="urn:agentpay:seller:abc",
            secret=SECRET,
        )
        verdict, reason, _ = check(quote, pricelist, SECRET)
        assert verdict is Verdict.REJECT
        assert reason is VerdictReason.UNKNOWN_ITEM

    def test_tampered_item_rejected_before_catalog_lookup(self) -> None:
        """Подмена позиции ломает подпись раньше, чем каталог успевает
        ответить, — порядок проверок имеет значение."""
        from dataclasses import replace

        pricelist = make_pricelist()
        quote = replace(make_quote(pricelist), item="выдуманный-продукт")
        verdict, reason, _ = check(quote, pricelist, SECRET)
        assert verdict is Verdict.REJECT
        assert reason is VerdictReason.SIGNATURE_INVALID

    def test_currency_mismatch_rejected(self) -> None:
        pricelist = make_pricelist()
        quote = make_quote(pricelist, currency="EUR")
        verdict, reason, _ = check(quote, pricelist, SECRET)
        assert verdict is Verdict.REJECT
        assert reason is VerdictReason.CURRENCY_MISMATCH

    def test_rail_mismatch_rejected(self) -> None:
        pricelist = make_pricelist()
        quote = make_quote(pricelist, rail="x402")
        verdict, reason, _ = check(
            quote, pricelist, SECRET, expect_rail="mock"
        )
        assert verdict is Verdict.REJECT
        assert reason is VerdictReason.RAIL_MISMATCH

    def test_resource_binding_enforced(self) -> None:
        pricelist = make_pricelist()
        quote = make_quote(pricelist)
        verdict, reason, _ = check(
            quote,
            pricelist,
            SECRET,
            expected_resource="urn:agentpay:другой:zzz",
        )
        assert verdict is Verdict.REJECT
        assert reason is VerdictReason.RESOURCE_UNBOUND

    def test_verdict_set_is_closed(self) -> None:
        assert {v.value for v in Verdict} == {"accept", "review", "reject"}


class TestSettleAmount:
    def test_exact_scheme_charges_fixed_amount(self) -> None:
        quote = make_quote(make_pricelist())
        assert settle_amount(quote, Decimal("0.5")) == quote.amount

    def test_upto_scheme_caps_at_ceiling(self) -> None:
        pricelist = make_pricelist()
        ceiling = pricelist.upto("clause.extract")
        quote = make_quote(
            pricelist, scheme=SCHEME_UPTO, max_amount=ceiling
        )
        assert settle_amount(quote, ceiling * 5) == ceiling

    def test_upto_scheme_charges_actual(self) -> None:
        pricelist = make_pricelist()
        quote = make_quote(
            pricelist,
            scheme=SCHEME_UPTO,
            max_amount=pricelist.upto("clause.extract"),
        )
        assert settle_amount(quote, Decimal("0.012")) == Decimal("0.012")

    def test_negative_outcome_rejected(self) -> None:
        quote = make_quote(make_pricelist())
        with pytest.raises(ValueError, match="отрицательно"):
            settle_amount(quote, Decimal("-1"))


class TestRails:
    def test_registry_has_builtin_rails(self) -> None:
        assert available() == ["mandate", "mock", "x402"]
        assert get("mock") is MockRail

    def test_unknown_rail_error_lists_options(self) -> None:
        with pytest.raises(KeyError, match="mandate"):
            get("нет-такого")

    def test_duplicate_registration_rejected(self) -> None:
        with pytest.raises(ValueError, match="уже зарегистрирован"):
            from agentpay import register

            register(MockRail)

    def test_x402_end_to_end(self) -> None:
        pricelist = make_pricelist()
        rail = X402Rail(pricelist, SECRET)
        quote = make_quote(pricelist, rail="x402")
        proof = rail.build_authorization(quote)
        assert rail.verify_authorization(proof) is Verdict.ACCEPT
        settlement = rail.settle(proof, Decimal("2.00"))
        assert settlement.settled == quote.amount
        assert settlement.status == "settled"

    def test_x402_blocks_replay(self) -> None:
        pricelist = make_pricelist()
        rail = X402Rail(pricelist, SECRET)
        proof = rail.build_authorization(make_quote(pricelist, rail="x402"))
        assert rail.verify_authorization(proof) is Verdict.ACCEPT
        assert rail.verify_authorization(proof) is Verdict.REJECT

    def test_mock_rail_needs_no_network(self) -> None:
        pricelist = make_pricelist()
        rail = MockRail(pricelist, SECRET)
        proof = rail.build_authorization(make_quote(pricelist))
        assert rail.verify_authorization(proof) is Verdict.ACCEPT

    def test_quote_for_other_rail_rejected_by_rail(self) -> None:
        pricelist = make_pricelist()
        rail = MockRail(pricelist, SECRET)
        proof = rail.build_authorization(make_quote(pricelist, rail="x402"))
        assert rail.verify_authorization(proof) is Verdict.REJECT

    def test_rail_cannot_change_price(self) -> None:
        """Ядро не доверяет сумме из доказательства: рельс сверяет её
        с каталогом, поэтому занижение цены не проходит."""
        pricelist = make_pricelist()
        rail = MockRail(pricelist, SECRET)
        quote = make_quote(pricelist, amount=Decimal("0.01"))
        proof = rail.build_authorization(quote)
        assert rail.verify_authorization(proof) is Verdict.REVIEW
        assert pricelist.price_of("clause.extract") == Decimal("4.48")

    def test_shared_nonce_store_blocks_across_instances(self) -> None:
        pricelist = make_pricelist()
        store = MemoryNonceStore()
        first = MockRail(pricelist, SECRET, nonces=store)
        second = MockRail(pricelist, SECRET, nonces=store)
        proof = first.build_authorization(make_quote(pricelist))
        assert first.verify_authorization(proof) is Verdict.ACCEPT
        assert second.verify_authorization(proof) is Verdict.REJECT


class TestNonceStore:
    def test_store_seen_after_remember(self) -> None:
        store = MemoryNonceStore()
        assert store.seen("n") is False
        store.remember("n", expires_at=9_999_999_999)
        assert store.seen("n") is True

    def test_base_class_is_abstract(self) -> None:
        store = NonceStore()
        with pytest.raises(NotImplementedError):
            store.seen("n")
        with pytest.raises(NotImplementedError):
            store.remember("n", 1)
        with pytest.raises(NotImplementedError):
            store.claim("n", 1)
        with pytest.raises(NotImplementedError):
            store.forget("n")

    def test_claim_grants_exactly_once(self) -> None:
        """Claim, в отличие от seen+remember, не оставляет окна для второй
        авторизации: первый захват выигрывает, последующие проигрывают."""
        store = MemoryNonceStore()
        assert store.claim("n", 9_999_999_999) is True
        assert store.claim("n", 9_999_999_999) is False

    def test_forget_releases_nonce_after_failed_work(self) -> None:
        """Ядро не отработало — авторизация не израсходована: покупатель
        не должен платить за неудачную попытку."""
        store = MemoryNonceStore()
        assert store.claim("n", 9_999_999_999) is True
        store.forget("n")
        assert store.seen("n") is False
        assert store.claim("n", 9_999_999_999) is True

    def test_forget_is_idempotent(self) -> None:
        store = MemoryNonceStore()
        store.forget("absent")
        assert store.claim("n", 9_999_999_999) is True

    def test_expired_nonce_can_be_claimed_again(self) -> None:
        store = MemoryNonceStore()
        assert store.claim("n", 1) is True
        assert store.claim("n", 9_999_999_999) is True

    def test_concurrent_claim_has_single_winner(self) -> None:
        """Гонка настоящая, а не смоделированная: восемь потоков одновременно
        бьются за один nonce, и выиграть должен ровно один."""
        store = MemoryNonceStore()
        barrier = threading.Barrier(8)
        wins: list[bool] = []
        guard = threading.Lock()

        def attempt() -> None:
            barrier.wait()
            got = store.claim("race", 9_999_999_999)
            with guard:
                wins.append(got)

        threads = [threading.Thread(target=attempt) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert sum(wins) == 1


class TestMandate:
    def test_mandate_allows_multiple_calls_under_limit(self) -> None:
        pricelist = make_pricelist()
        price = pricelist.price_of("clause.extract")
        rail = MandateRail(pricelist, SECRET, spending_limit=Decimal("10"))
        first = rail.build_authorization(
            make_quote(pricelist, rail="mandate", quote_id="q-1")
        )
        second = rail.build_authorization(
            make_quote(pricelist, rail="mandate", quote_id="q-2")
        )
        assert rail.verify_authorization(first) is Verdict.ACCEPT
        assert rail.verify_authorization(second) is Verdict.ACCEPT
        rail.settle(first, price)
        rail.settle(second, price)
        assert rail.spent() == price * 2

    def test_mandate_is_multi_use_where_one_time_rail_would_replay(self) -> None:
        """Основание, по которому мандату не нужна одноразовость nonce."""
        pricelist = make_pricelist()
        price = pricelist.price_of("clause.extract")
        once = MockRail(pricelist, SECRET)
        repeated = MandateRail(pricelist, SECRET, spending_limit=Decimal("20"))
        same = make_quote(pricelist, rail="mock")
        proof = once.build_authorization(same)
        assert once.verify_authorization(proof) is Verdict.ACCEPT
        assert once.verify_authorization(proof) is Verdict.REJECT
        quote = make_quote(pricelist, rail="mandate", quote_id="q-1")
        first = repeated.build_authorization(quote)
        second = repeated.build_authorization(
            make_quote(pricelist, rail="mandate", quote_id="q-2")
        )
        assert repeated.verify_authorization(first) is Verdict.ACCEPT
        assert repeated.verify_authorization(second) is Verdict.ACCEPT
        assert price == Decimal("4.48")

    def test_mandate_blocks_when_limit_would_be_exceeded(self) -> None:
        pricelist = make_pricelist()
        price = pricelist.price_of("clause.extract")
        rail = MandateRail(pricelist, SECRET, spending_limit=price)
        first = rail.build_authorization(
            make_quote(pricelist, rail="mandate", quote_id="q-1")
        )
        second = rail.build_authorization(
            make_quote(pricelist, rail="mandate", quote_id="q-2")
        )
        assert rail.verify_authorization(first) is Verdict.ACCEPT
        rail.settle(first, price)
        assert rail.spent() == price
        assert rail.verify_authorization(second) is Verdict.REJECT

    def test_mandate_settlement_capped_by_remaining_room(self) -> None:
        pricelist = make_pricelist()
        price = pricelist.price_of("clause.extract")
        limit = price + Decimal("0.52")
        rail = MandateRail(pricelist, SECRET, spending_limit=limit)
        first = rail.build_authorization(
            make_quote(pricelist, rail="mandate", quote_id="q-1")
        )
        second = rail.build_authorization(
            make_quote(pricelist, rail="mandate", quote_id="q-2")
        )
        assert rail.settle(first, price).settled == price
        remainder = rail.settle(second, price)
        assert remainder.settled == Decimal("0.52")
        assert remainder.status == "settled"
        assert rail.spent() == limit

    def test_mandate_exhausted_limit_settles_zero(self) -> None:
        pricelist = make_pricelist()
        rail = MandateRail(pricelist, SECRET, spending_limit=Decimal("1"))
        proof = rail.build_authorization(
            make_quote(pricelist, rail="mandate", quote_id="q-1")
        )
        rail._spent[rail.mandate_id] = Decimal("1")
        settlement = rail.settle(proof, Decimal("4.48"))
        assert settlement.settled == Decimal(0)
        assert settlement.status == "limit_exhausted"

    def test_reused_quote_goes_to_review(self) -> None:
        pricelist = make_pricelist()
        price = pricelist.price_of("clause.extract")
        rail = MandateRail(pricelist, SECRET, spending_limit=Decimal("100"))
        proof = rail.build_authorization(
            make_quote(pricelist, rail="mandate", quote_id="q-same")
        )
        assert rail.verify_authorization(proof) is Verdict.ACCEPT
        rail.settle(proof, price)
        assert rail.verify_authorization(proof) is Verdict.REVIEW

    def test_mandate_without_limit_never_blocks_on_spend(self) -> None:
        pricelist = make_pricelist()
        rail = MandateRail(pricelist, SECRET)
        for index in range(5):
            proof = rail.build_authorization(
                make_quote(pricelist, rail="mandate", quote_id=f"q-{index}")
            )
            assert rail.verify_authorization(proof) is Verdict.ACCEPT
            rail.settle(proof, Decimal("4.48"))
        assert rail.spent() == Decimal("22.40")

    def test_mandate_limit_counts_unsettled_authorized_calls(self) -> None:
        """Лимит обязан видеть и нерассчитанные авторизации.

        Считать только потраченное нельзя: десяток параллельных вызовов проходит
        одну и ту же проверку на полном остатке, работа выполняется десять раз,
        а деньги списываются один. Резерв при авторизации закрывает эту дыру.
        """
        pricelist = make_pricelist()
        price = pricelist.price_of("clause.extract")
        rail = MandateRail(pricelist, SECRET, spending_limit=price * 2)
        proofs = [
            rail.build_authorization(
                make_quote(pricelist, rail="mandate", quote_id=f"q-{index}")
            )
            for index in range(3)
        ]
        assert rail.verify_authorization(proofs[0]) is Verdict.ACCEPT
        assert rail.verify_authorization(proofs[1]) is Verdict.ACCEPT
        # Третий не влезает в лимит, хотя не потрачено ещё ни цента.
        assert rail.verify_authorization(proofs[2]) is Verdict.REJECT
        assert rail.spent() == Decimal(0)
        assert rail.reserved() == price * 2

    def test_mandate_reservation_is_released_by_settlement(self) -> None:
        pricelist = make_pricelist()
        price = pricelist.price_of("clause.extract")
        rail = MandateRail(pricelist, SECRET, spending_limit=price * 2)
        first = rail.build_authorization(
            make_quote(pricelist, rail="mandate", quote_id="q-1")
        )
        second = rail.build_authorization(
            make_quote(pricelist, rail="mandate", quote_id="q-2")
        )
        assert rail.verify_authorization(first) is Verdict.ACCEPT
        assert rail.verify_authorization(second) is Verdict.ACCEPT
        rail.settle(first, price)
        # Резерв первой котировки погашен списанием, второй ещё в резерве.
        assert rail.reserved() == price
        assert rail.spent() == price
        third = rail.build_authorization(
            make_quote(pricelist, rail="mandate", quote_id="q-3")
        )
        assert rail.verify_authorization(third) is Verdict.REJECT

    def test_mandate_repeated_verification_of_same_quote_is_idempotent(self) -> None:
        """Повторная проверка одной и той же котировки не должна ни удваивать
        резерв, ни отклоняться как новая покупка."""
        pricelist = make_pricelist()
        price = pricelist.price_of("clause.extract")
        rail = MandateRail(pricelist, SECRET, spending_limit=price)
        proof = rail.build_authorization(
            make_quote(pricelist, rail="mandate", quote_id="q-1")
        )
        assert rail.verify_authorization(proof) is Verdict.ACCEPT
        assert rail.verify_authorization(proof) is Verdict.ACCEPT
        assert rail.reserved() == price
        rail.settle(proof, price)
        assert rail.reserved() == Decimal(0)
        assert rail.spent() == price

    def test_mandate_rejected_verification_holds_no_reservation(self) -> None:
        pricelist = make_pricelist()
        rail = MandateRail(pricelist, SECRET, spending_limit=Decimal("0.01"))
        proof = rail.build_authorization(
            make_quote(pricelist, rail="mandate", quote_id="q-1")
        )
        assert rail.verify_authorization(proof) is Verdict.REJECT
        assert rail.reserved() == Decimal(0)
