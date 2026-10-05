"""Тесты CLI: проверка подписи должна реально падать при чужом ключе.

Отдельный файл, потому что здесь важна не арифметика, а поведение
интерфейса: код возврата и различие ключей выпуска и проверки.
"""

from __future__ import annotations

import json
from contextlib import redirect_stdout
from io import StringIO

import pytest

from agentpay.cli import build_parser, demo_pricelist, main


@pytest.fixture(autouse=True)
def env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTPAY_SECRET", "issue-secret")
    monkeypatch.setenv("AGENTPAY_SALT", "vendor-01")
    # Ключ проверки не должен просачиваться из окружения разработчика:
    # CLI читает AGENTPAY_VERIFY_SECRET, и незаданная здесь переменная
    # делала бы тесты зависимыми от того, как запущен pytest.
    monkeypatch.delenv("AGENTPAY_VERIFY_SECRET", raising=False)


def run(*argv: str) -> tuple[int, dict]:
    buffer = StringIO()
    with redirect_stdout(buffer):
        code = main(list(argv))
    output = buffer.getvalue().strip()
    return code, json.loads(output) if output.startswith("{") else {}


def run_text(*argv: str) -> tuple[int, str]:
    buffer = StringIO()
    with redirect_stdout(buffer):
        code = main(list(argv))
    return code, buffer.getvalue()


class TestCatalog:
    def test_price_command_prints_margin_table(self) -> None:
        code, out = run_text("price")
        assert code == 0
        assert "clause.extract" in out
        assert "маржа" in out

    def test_demo_rates_are_not_claimed_verified(self) -> None:
        """Демо-каталог не имеет источника, поэтому обязан ругаться."""
        code, out = run_text("price")
        assert "PRICE TO VERIFY" in out
        assert demo_pricelist().unverified_rates() is True

    def test_price_json_shape(self) -> None:
        code, out = run("price", "--format", "json")
        assert code == 0
        assert out["rates_verified"] is False
        assert set(out["prices"]) == {
            "clause.extract",
            "defi.snapshot",
            "solidity.verify",
        }
        assert Decimal_price(out) > 0

    def test_rails_listed(self) -> None:
        code, out = run_text("rails")
        assert code == 0
        assert out.split() == ["mandate", "mock", "x402"]


def Decimal_price(payload: dict) -> float:
    from decimal import Decimal

    return float(Decimal(payload["prices"]["clause.extract"]["price"]))


class TestQuoteCommand:
    def test_verify_accepts_correct_key(self) -> None:
        code, out = run("quote", "clause.extract", "--verify")
        assert code == 0
        assert out["verdict"] == "accept"
        assert out["signed"] is True
        assert out["resource"].startswith("urn:agentpay:seller:")

    def test_verify_fails_on_foreign_verification_key(self) -> None:
        """Ключ выпуска и ключ проверки различаются: подделка ловится."""
        code, out = run(
            "quote",
            "clause.extract",
            "--verify",
            "--verify-secret",
            "someone-elses-key",
        )
        assert code == 1
        assert out["verdict"] == "reject"
        assert out["signed"] is False

    def test_verification_key_from_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Ключ проверки из окружения: подпись выпуска ему не соответствует."""
        monkeypatch.setenv("AGENTPAY_VERIFY_SECRET", "env-verify")
        code, out = run("quote", "clause.extract", "--verify")
        assert code == 1
        assert out["verdict"] == "reject"
        assert out["signed"] is False

    def test_matching_verification_key_from_environment_accepts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AGENTPAY_VERIFY_SECRET", "issue-secret")
        code, out = run("quote", "clause.extract", "--verify")
        assert code == 0
        assert out["verdict"] == "accept"

    def test_settle_charges_quoted_amount(self) -> None:
        code, out = run("quote", "clause.extract", "--settle", "0.005")
        assert code == 0
        assert out["settlement"]["status"] == "settled"
        assert out["settlement"]["settled"] == out["amount"]

    def test_upto_overcharge_is_capped_and_flagged(self) -> None:
        code, out = run(
            "quote", "clause.extract", "--scheme", "upto", "--settle", "500"
        )
        assert code == 0
        assert out["settlement"]["settled"] == out["amount"]
        assert "выше потолка" in out["warning"]

    def test_upto_under_ceiling_charges_actual(self) -> None:
        code, out = run(
            "quote", "clause.extract", "--scheme", "upto", "--settle", "0.005"
        )
        assert code == 0
        assert out["settlement"]["settled"] == "0.005"
        assert "warning" not in out

    def test_unknown_item_exits_with_code_two(self) -> None:
        code, _ = run("quote", "выдуманный", "--verify")
        assert code == 2

    def test_resource_depends_on_salt(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _, first = run("quote", "clause.extract")
        monkeypatch.setenv("AGENTPAY_SALT", "vendor-02")
        _, second = run("quote", "clause.extract")
        assert first["resource"] != second["resource"]
        assert first["digest"] != second["digest"]

    def test_digest_and_nonce_differ_between_calls(self) -> None:
        """Каждая котировка уникальна: digest включает nonce, поэтому
        повторное предъявление отсекается на уровне подписи."""
        _, first = run("quote", "clause.extract")
        _, second = run("quote", "clause.extract")
        assert first["nonce"] != second["nonce"]
        assert first["digest"] != second["digest"]
        assert first["signature"] != second["signature"]

    def test_ttl_reflected_in_expiry(self) -> None:
        import time as time_module

        before = int(time_module.time())
        _, out = run("quote", "clause.extract", "--ttl", "5")
        assert out["expires_at"] - before <= 6
        assert out["expires_at"] > before

    def test_parser_requires_subcommand(self) -> None:
        with pytest.raises(SystemExit):
            build_parser().parse_args([])

    def test_negative_ttl_rejected(self) -> None:
        with pytest.raises(ValueError, match="ttl_seconds"):
            run("quote", "clause.extract", "--ttl", "0")
