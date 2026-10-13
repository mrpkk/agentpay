#!/usr/bin/env python3
"""Тесты HTTP-витрины agentpay.

Регрессия, из-за которой этот файл существует. Сериализация `/pricing`
читала поля `rate` и `unit`, которых у `PriceItem` нет: обработчик падал
с AttributeError, а покупатель по публичному адресу получал 502. Тесты
на модель цены такие ошибки не ловят — ловит только запрос к витрине.

Витрина, которая падает, хуже отсутствующей: покупатель уходит с
впечатлением «сервис мёртв», а не «товара пока нет».
"""

from __future__ import annotations

import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from http_server import Handler  # noqa: E402


class _Live:
    """Поднимает обработчик на свободном порту."""

    def __enter__(self):
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.srv.serve_forever,
                                       daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"
        return self

    def __exit__(self, *_exc):
        self.srv.shutdown()
        self.srv.server_close()

    def get(self, path: str):
        try:
            with urllib.request.urlopen(f"{self.base}{path}", timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())


class PricingEndpointTests(unittest.TestCase):
    def setUp(self):
        self.live = _Live()
        self.live.__enter__()
        self.addCleanup(self.live.__exit__, None, None, None)

    def test_pricing_returns_200(self):
        code, body = self.live.get("/pricing")
        self.assertEqual(code, 200, body)

    def test_catalog_is_not_empty(self):
        _, body = self.live.get("/pricing")
        self.assertTrue(body["items"], "витрина без товара")

    def test_items_carry_price_and_cost(self):
        _, body = self.live.get("/pricing")
        for item in body["items"]:
            self.assertIn("price", item, item["name"])
            self.assertIn("cost", item, item["name"])

    def test_no_removed_field_reappears(self):
        # `rate` больше не существует у модели. Если вернётся — снова
        # AttributeError, и это молча сломает витрину.
        _, body = self.live.get("/pricing")
        for item in body["items"]:
            self.assertNotIn("rate", item, item["name"])

    def test_usage_breakdown_reaches_the_buyer(self):
        # Продаём «честное распределение ресурсов». Разложение обязано
        # доходить до покупателя, иначе это лозунг, а не товар.
        _, body = self.live.get("/pricing")
        for item in body["items"]:
            self.assertIn("usage", item, item["name"])
            self.assertIn("tokens", item["usage"])

    def test_currency_and_verification_are_declared(self):
        _, body = self.live.get("/pricing")
        self.assertIn("currency", body)
        self.assertIn("verified", body)

    def test_health_is_public(self):
        code, body = self.live.get("/health")
        self.assertEqual(code, 200, body)


if __name__ == "__main__":
    unittest.main()
