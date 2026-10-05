"""Тесты денежного пути на реальном ядре AILegal и реальном золотом корпусе.

Здесь нет заглушек: тесты падают, если AILegal недоступен или изменился.
Это осознанно — молчащая подмена ядра в тесте денежного пути опаснее
упавшего теста.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from decimal import Decimal
from pathlib import Path

import pytest

from agentpay.catalog import ProviderUnavailable, load_catalog
from agentpay.cores import CoreUnavailable, load_ailegal_core
from agentpay.ledger import DuplicateSettlement, Ledger, Receipt
from agentpay.pipeline import PaidCall, UnpricedCatalog
from agentpay.verify import MemoryNonceStore

SECRET = b"integration-secret"
CATALOG_EXAMPLE = Path("pricelist/ailegal.example.toml")
CATALOG_PLACEHOLDER = Path("pricelist/ailegal.toml")

CLAUSES = [
    {"type": "Confidentiality", "risk_level": "MEDIUM"},
    {"type": "Limitation of Liability", "risk_level": "HIGH"},
    {"type": "Termination", "risk_level": "CRITICAL"},
]


@pytest.fixture(scope="module")
def core():
    return load_ailegal_core()


@pytest.fixture(scope="module")
def catalog():
    return load_catalog(CATALOG_EXAMPLE)


class TestRealCore:
    def test_core_loads_real_aillegal_modules(self, core) -> None:
        assert core.root.endswith("services")
        assert len(core.clause_types()) > 50
        assert len(core.categories()) == 10

    def test_golden_corpus_is_reproduced_exactly(self, core) -> None:
        report = core.run_golden()
        assert report["cases"] == 50
        assert report["checked"] == 50
        assert report["mismatches"] == []
        assert report["accuracy"] == Decimal(1)

    def test_compliance_status_is_passed_through(self, core) -> None:
        """Кейсы корпуса со штрафом воспроизводятся только с compliance_status.
        Игнорирование этого поля даёт ложные 'расхождения'."""
        without = core.clause_risk(
            [{"risk_level": "MEDIUM", "category": "FINANCIAL"}]
        )
        with_status = core.clause_risk(
            [{"risk_level": "MEDIUM", "category": "FINANCIAL"}],
            "NON_COMPLIANT",
        )
        assert without["risk_level"] == "MEDIUM"
        assert with_status["risk_level"] == "HIGH"

    def test_categorize_falls_back_to_procedural(self, core) -> None:
        assert core.categorize({"type": "Несуществующий тип"}) == "PROCEDURAL"

    def test_missing_core_raises_instead_of_stubbing(self, tmp_path: Path) -> None:
        with pytest.raises(CoreUnavailable, match="не найден"):
            load_ailegal_core(tmp_path)

    def test_structural_change_detected(self, tmp_path: Path) -> None:
        (tmp_path / "clause_dna.py").write_text("X = 1\n", encoding="utf-8")
        (tmp_path / "risk_aggregation.py").write_text(
            "def document_risk_v2():\n    return {}\n", encoding="utf-8"
        )
        with pytest.raises(CoreUnavailable, match="CLAUSE_TYPES"):
            load_ailegal_core(tmp_path)


class TestCatalogIntegrity:
    def test_placeholder_catalog_is_unverified_and_free(self) -> None:
        catalog = load_catalog(CATALOG_PLACEHOLDER)
        assert catalog.pricelist.unverified_rates() is True
        assert catalog.pricelist.price_of("ailegal.clause.risk") == Decimal(0)

    def test_example_catalog_flags_provider_items(self, catalog) -> None:
        assert "ailegal.clause.extract" in catalog.provider_items()
        assert "ailegal.clause.extract" not in catalog.offline_items()

    def test_upto_scheme_declared_in_catalog(self, catalog) -> None:
        assert catalog.scheme_of("ailegal.clause.extract") == "upto"
        assert catalog.scheme_of("ailegal.clause.risk") == "exact"

    def test_every_deterministic_item_is_offline(self, catalog) -> None:
        for name in catalog.offline_items():
            assert catalog.item_meta(name).scheme == "exact"


class TestPaidCall:
    def test_full_path_executes_real_core(self, catalog, core) -> None:
        call = PaidCall(catalog, "mock", SECRET)
        outcome = call.run(
            "ailegal.clause.risk",
            "urn:agentpay:ailegal:case-1",
            lambda: core.clause_risk(CLAUSES),
            allow_unverified=True,
        )
        assert outcome.verdict == "accept"
        assert outcome.executed is True
        assert outcome.result["risk_level"] in {"MEDIUM", "HIGH", "CRITICAL"}
        assert outcome.settled == outcome.quote.amount
        assert outcome.cost < outcome.settled

    def test_result_is_real_ailegal_verdict(self, catalog, core) -> None:
        expected = core.clause_risk(CLAUSES)
        call = PaidCall(catalog, "mock", SECRET)
        outcome = call.run(
            "ailegal.clause.risk",
            "urn:agentpay:ailegal:case-2",
            lambda: core.clause_risk(CLAUSES),
            allow_unverified=True,
        )
        assert outcome.result == expected

    def test_measured_usage_is_recorded(self, catalog, core) -> None:
        call = PaidCall(catalog, "mock", SECRET)
        outcome = call.run(
            "ailegal.clause.risk",
            "urn:agentpay:ailegal:case-3",
            lambda: core.clause_risk(CLAUSES),
            allow_unverified=True,
        )
        assert outcome.measurement.usage.cpu_seconds > 0
        assert outcome.measurement.usage.bytes_out > 0
        assert outcome.receipt is None

    def test_unverified_catalog_refuses_to_sell(self, core) -> None:
        catalog = load_catalog(CATALOG_PLACEHOLDER)
        call = PaidCall(catalog, "mock", SECRET)
        with pytest.raises(UnpricedCatalog, match="не подтверждены"):
            call.run(
                "ailegal.clause.risk",
                "urn:agentpay:ailegal:case-4",
                lambda: core.clause_risk(CLAUSES),
            )

    def test_refusal_happens_before_execution(self, catalog) -> None:
        """Ядро не должно выполняться, если продажа запрещена."""
        calls = []

        def spy():
            calls.append(1)
            return {}

        call = PaidCall(catalog, "mock", SECRET)
        with pytest.raises(UnpricedCatalog):
            call.run(
                "ailegal.clause.risk",
                "urn:agentpay:ailegal:case-5",
                spy,
            )
        assert calls == []

    def test_distinct_resources_settle_independently(
        self, catalog, core, tmp_path
    ) -> None:
        """Привязка к ресурсу: разные ресурсы — разные котировки, ложного
        replay не возникает, но каждая списывается отдельно."""
        ledger = Ledger(tmp_path / "ledger.jsonl")
        call = PaidCall(catalog, "mock", SECRET, ledger=ledger)
        ids = []
        for index in range(3):
            outcome = call.run(
                "ailegal.clause.risk",
                f"urn:agentpay:ailegal:contract-{index}",
                lambda: core.clause_risk(CLAUSES),
                allow_unverified=True,
            )
            assert outcome.verdict == "accept"
            ids.append(outcome.quote.quote_id)
        assert len(set(ids)) == 3
        assert len(ledger) == 3
        assert ledger.total() == sum(
            Decimal(r.settled_amount) for r in ledger
        )

    def test_quote_is_bound_to_requested_resource(self, catalog) -> None:
        resource = "urn:agentpay:ailegal:contract-bound"
        quote = PaidCall(catalog, "mock", SECRET).quote_for(
            "ailegal.clause.risk", resource
        )
        assert quote.resource == resource
        other = PaidCall(catalog, "mock", SECRET).quote_for(
            "ailegal.clause.risk", "urn:agentpay:ailegal:contract-other"
        )
        assert quote.quote_id != other.quote_id

    def test_duplicate_settlement_blocked(self, catalog, core, tmp_path: Path) -> None:
        ledger = Ledger(tmp_path / "ledger.jsonl")
        call = PaidCall(catalog, "mock", SECRET, ledger=ledger)
        resource = "urn:agentpay:ailegal:case-8"
        first = call.run(
            "ailegal.clause.risk",
            resource,
            lambda: core.clause_risk(CLAUSES),
            allow_unverified=True,
        )
        assert first.receipt is not None
        with pytest.raises(DuplicateSettlement):
            call.run(
                "ailegal.clause.risk",
                resource,
                lambda: core.clause_risk(CLAUSES),
                allow_unverified=True,
            )
        assert len(ledger) == 1

    def test_duplicate_returns_existing_receipt(self, catalog, core, tmp_path) -> None:
        ledger = Ledger(tmp_path / "ledger.jsonl")
        resource = "urn:agentpay:ailegal:case-9"
        call = PaidCall(catalog, "mock", SECRET, ledger=ledger)
        call.run(
            "ailegal.clause.risk",
            resource,
            lambda: core.clause_risk(CLAUSES),
            allow_unverified=True,
        )
        again = call.run(
            "ailegal.clause.risk",
            resource,
            lambda: core.clause_risk(CLAUSES),
            allow_unverified=True,
            on_duplicate="return",
        )
        assert again.receipt is not None
        assert len(ledger) == 1

    def test_failed_core_releases_authorization(self, catalog, tmp_path) -> None:
        """Ядро упало — деньги не должны быть удержаны.

        Claim происходит до исполнения, поэтому без явного отката покупатель
        потерял бы и деньги, и результат. Повторная попытка обязана пройти.
        """
        ledger = Ledger(tmp_path / "ledger.jsonl")
        store = MemoryNonceStore()
        call = PaidCall(
            catalog, "mock", SECRET, ledger=ledger, nonces=store
        )

        def boom() -> None:
            raise RuntimeError("ядро упало")

        with pytest.raises(RuntimeError, match="ядро упало"):
            call.run(
                "ailegal.clause.risk",
                "urn:agentpay:ailegal:case-10",
                boom,
                allow_unverified=True,
            )
        assert len(ledger) == 0

        recovered = call.run(
            "ailegal.clause.risk",
            "urn:agentpay:ailegal:case-10",
            lambda: {"ok": True},
            allow_unverified=True,
        )
        assert recovered.executed is True
        assert recovered.verdict == "accept"
        assert len(ledger) == 1

    def test_concurrent_calls_do_not_double_charge(
        self, catalog, core, tmp_path
    ) -> None:
        """Параллельные вызовы одного ресурса исполняются ровно один раз.

        Проверка одноразовости сама по себе не запрещает исполнение: два
        потока читают одно и то же состояние и оба проходят проверку. Claim
        обязан оставить ровно одного победителя, а ledger — одну запись.
        """
        ledger = Ledger(tmp_path / "ledger.jsonl")
        store = MemoryNonceStore()
        call = PaidCall(
            catalog, "mock", SECRET, ledger=ledger, nonces=store
        )
        resource = "urn:agentpay:ailegal:case-11"
        barrier = threading.Barrier(4)
        executed: list[int] = []
        guard = threading.Lock()
        failures: list[BaseException] = []

        def work() -> dict:
            with guard:
                executed.append(1)
            # Держим исполнение открытым: без этого окно между проверкой
            # дубля и записью квитанции слишком узкое, и гонка проходит
            # случайно, а тест ничего не доказывает.
            time.sleep(0.05)
            return {"ok": True}

        def attempt() -> None:
            barrier.wait()
            try:
                call.run(
                    "ailegal.clause.risk",
                    resource,
                    work,
                    allow_unverified=True,
                    on_duplicate="return",
                )
            except BaseException as exc:  # noqa: BLE001 - фиксируем гонку
                failures.append(exc)

        threads = [threading.Thread(target=attempt) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert failures == []
        assert len(ledger) == 1
        # Главное утверждение: работу выполнил ровно один поток, а не четыре.
        assert len(executed) == 1

    def test_mandate_limit_guards_work_in_full_pipeline(
        self, catalog, tmp_path
    ) -> None:
        """Лимит мандата обязан защищать и труд, а не только деньги.

        Раньше контур проверял котировку сам, минуя рельс, поэтому резерв
        лимита в нём не действовал: работа выполнялась, а списать лимит
        давали не больше одной суммы. Здесь рельс — источник решения,
        и лимит держит уже на этапе исполнения.
        """
        ledger = Ledger(tmp_path / "ledger.jsonl")
        price = catalog.pricelist.price_of("ailegal.clause.risk")
        call = PaidCall(
            catalog,
            "mandate",
            SECRET,
            ledger=ledger,
            spending_limit=price,
            mandate_id="m-pipeline",
        )
        executed: list[str] = []

        def work() -> dict:
            executed.append("done")
            return {"ok": True}

        first = call.run(
            "ailegal.clause.risk",
            "urn:agentpay:ailegal:mandate-1",
            work,
            allow_unverified=True,
        )
        assert first.verdict == "accept"
        assert executed == ["done"]

        # Разные ресурсы — разные котировки, но лимит уже исчерпан.
        second = call.run(
            "ailegal.clause.risk",
            "urn:agentpay:ailegal:mandate-2",
            work,
            allow_unverified=True,
        )
        assert second.verdict == "reject"
        assert second.executed is False
        assert executed == ["done"]
        assert len(ledger) == 1

    def test_ledger_records_real_measurement(self, catalog, core, tmp_path) -> None:
        ledger = Ledger(tmp_path / "ledger.jsonl")
        call = PaidCall(catalog, "mock", SECRET, ledger=ledger)
        outcome = call.run(
            "ailegal.clause.risk",
            "urn:agentpay:ailegal:case-10",
            lambda: core.clause_risk(CLAUSES),
            allow_unverified=True,
        )
        assert outcome.receipt is not None
        receipt = ledger.find(outcome.quote.quote_id)
        assert receipt is not None
        assert Decimal(receipt.cost) == outcome.cost
        assert receipt.verdict == "accept"
        assert receipt.usage["cpu_seconds"]
        assert len(receipt.result_digest) == 64


class TestLedger:
    def test_append_and_iterate(self, tmp_path: Path) -> None:
        ledger = Ledger(tmp_path / "l.jsonl")
        for index in range(3):
            ledger.append(
                Receipt(
                    quote_id=f"q-{index}",
                    item="ailegal.clause.risk",
                    rail="mock",
                    scheme="exact",
                    quoted_amount="0.01",
                    settled_amount="0.01",
                    currency="USD",
                    verdict="accept",
                    cost="0.00001",
                    margin_at_settle="0.999",
                )
            )
        assert len(ledger) == 3
        assert ledger.total() == Decimal("0.03")

    def test_missing_file_is_empty_not_error(self, tmp_path: Path) -> None:
        ledger = Ledger(tmp_path / "нет.jsonl")
        assert len(ledger) == 0
        assert ledger.total() == Decimal(0)
        assert ledger.margin() is None

    def test_summary_groups_by_item(self, tmp_path: Path) -> None:
        ledger = Ledger(tmp_path / "l.jsonl")
        for index, item in enumerate(["a", "a", "b"]):
            ledger.append(
                Receipt(
                    quote_id=f"q-{index}",
                    item=item,
                    rail="mock",
                    scheme="exact",
                    quoted_amount="0.01",
                    settled_amount="0.01",
                    currency="USD",
                    verdict="accept",
                    cost="0.001",
                    margin_at_settle="0.9",
                )
            )
        summary = ledger.summary()
        assert summary["receipts"] == 3
        assert summary["by_item"] == {"a": "0.02", "b": "0.01"}

    def test_file_is_append_only(self, tmp_path: Path) -> None:
        path = tmp_path / "l.jsonl"
        ledger = Ledger(path)
        ledger.append(
            Receipt(
                quote_id="q-1",
                item="a",
                rail="mock",
                scheme="exact",
                quoted_amount="0.01",
                settled_amount="0.01",
                currency="USD",
                verdict="accept",
                cost="0.001",
                margin_at_settle="0.9",
            )
        )
        first = path.read_text(encoding="utf-8")
        ledger.append(
            Receipt(
                quote_id="q-2",
                item="a",
                rail="mock",
                scheme="exact",
                quoted_amount="0.01",
                settled_amount="0.01",
                currency="USD",
                verdict="accept",
                cost="0.001",
                margin_at_settle="0.9",
            )
        )
        second = path.read_text(encoding="utf-8")
        assert second.startswith(first)
        assert len(second.splitlines()) == 2


class TestGoldenCorpusFile:
    def test_corpus_is_the_real_aillegal_file(self) -> None:
        core = load_ailegal_core()
        report = core.run_golden()
        assert report["corpus"].endswith("AILegal/backend/corpus/golden.json")
        assert report["schema"] == "golden-corpus-v1"

    def test_limited_run_is_consistent(self, core) -> None:
        report = core.run_golden(limit=10)
        assert report["cases"] == 10
        assert report["checked"] == 10


class TestProviderGate:
    """Позиция с внешним провайдером не должна продаваться без него.

    До появления этих проверок `requires_provider` был декларацией в TOML,
    которую никто не читал: `ailegal.clause.extract` помечена
    `requires_provider = "gigachat"`, но вызов проходил, деньги списывались,
    а `tokens` в квитанции были нулевыми — работа не выполнялась вовсе.
    """

    def test_provider_item_is_declared_as_such(self, catalog) -> None:
        assert catalog.provider_of("ailegal.clause.extract") == "gigachat"
        assert catalog.provider_of("ailegal.clause.risk") == ""
        assert "ailegal.clause.extract" in catalog.provider_items()
        assert "ailegal.clause.extract" not in catalog.offline_items()

    def test_provider_item_is_refused_without_provider(
        self, catalog, tmp_path
    ) -> None:
        ledger = Ledger(tmp_path / "ledger.jsonl")
        call = PaidCall(catalog, "mock", SECRET, ledger=ledger)

        def work() -> dict:
            raise AssertionError("работа не должна была запускаться")

        with pytest.raises(ProviderUnavailable) as info:
            call.run(
                "ailegal.clause.extract",
                "urn:agentpay:ailegal:extract-1",
                work,
                allow_unverified=True,
            )
        assert "gigachat" in str(info.value)
        # Ни авторизации, ни денег: отказ обязан быть бесплатным для покупателя.
        assert len(ledger) == 0

    def test_offline_item_needs_no_provider(self, catalog, tmp_path) -> None:
        ledger = Ledger(tmp_path / "ledger.jsonl")
        call = PaidCall(catalog, "mock", SECRET, ledger=ledger)
        outcome = call.run(
            "ailegal.clause.risk",
            "urn:agentpay:ailegal:risk-1",
            lambda: {"risk_level": "HIGH"},
            allow_unverified=True,
        )
        assert outcome.executed is True
        assert len(ledger) == 1

    def test_declared_provider_unlocks_the_item(
        self, catalog, tmp_path
    ) -> None:
        """Явное перечисление провайдера снимает запрет — иначе гейт
        нельзя было бы открыть, не выкидывая позицию из каталога."""
        ledger = Ledger(tmp_path / "ledger.jsonl")
        call = PaidCall(catalog, "mock", SECRET, ledger=ledger)
        outcome = call.run(
            "ailegal.clause.extract",
            "urn:agentpay:ailegal:extract-2",
            lambda: {"clauses": []},
            allow_unverified=True,
            available_providers={"gigachat"},
        )
        assert outcome.executed is True
        assert len(ledger) == 1


class TestCliDispatch:
    """Позиция должна однозначно задавать исполняемую работу.

    Раньше `args.item` уходил только в котировку, а тело всегда было
    `core.clause_risk(clauses)`: любой запрошенный вызов возвращал
    агрегацию риска, включая «извлечение клауз». Покупатель платил за одну
    услугу и получал другую.
    """

    def _run_cli(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "agentpay.cli", *args],
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "AGENTPAY_SECRET": SECRET.decode(),
                "AGENTPAY_SALT": "test-seller",
            },
        )

    def test_provider_item_is_not_sold_by_cli(self) -> None:
        done = self._run_cli(
            "run",
            "ailegal.clause.extract",
            "--catalog",
            str(CATALOG_EXAMPLE),
            "--allow-unverified",
            "--resource",
            "urn:agentpay:ailegal:cli-extract",
        )
        assert done.returncode == 6, done.stdout + done.stderr
        assert "не имитируются" in done.stderr

    def test_unknown_item_is_refused(self) -> None:
        done = self._run_cli(
            "run",
            "ailegal.clause.hallucinated",
            "--catalog",
            str(CATALOG_EXAMPLE),
            "--allow-unverified",
        )
        assert done.returncode == 6
        assert "не имеет исполнения" in done.stderr

    def test_each_item_runs_its_own_work(self, core) -> None:
        """Каждая позиция исполняет свою работу, а не одну и ту же.

        CLI не печатает тело результата, поэтому различия проверяются по
        размеру ответа: у агрегации риска и у категоризации он разный.
        """
        sizes = {}
        for item in ("ailegal.clause.risk", "ailegal.clause.categorize"):
            done = self._run_cli(
                "run",
                item,
                "--catalog",
                str(CATALOG_EXAMPLE),
                "--allow-unverified",
                "--resource",
                f"urn:agentpay:ailegal:cli-{item}",
            )
            assert done.returncode == 0, done.stdout + done.stderr
            sizes[item] = json.loads(done.stdout)["usage"]["bytes_out"]

        assert sizes["ailegal.clause.risk"] != sizes["ailegal.clause.categorize"]
        # Тела работ различаются и на уровне ядра, а не только по размеру.
        risk = core.clause_risk(CLAUSES)
        assert "risk_level" in risk
        categorized = [core.categorize(clause) for clause in CLAUSES]
        assert all(isinstance(value, str) for value in categorized)
