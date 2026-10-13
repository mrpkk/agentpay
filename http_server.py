#!/usr/bin/env python3
"""HTTP: цена по факту, а не по объявлению.

agentpay умеет разложить себестоимость вызова на измеримые компоненты
(токены, CPU, байты, записи, внешние вызовы) и посчитать по ним цену.
Эта обёртка выставляет наружу ровно это: присылаешь израсходованное —
получаешь цену с разбивкой.

Ничего не оценивается на глаз: если компонент не передан, он считается
нулевым, и итог показывает, из чего конкретно сложилась сумма. Иначе
цифра выглядела бы точной, будучи догадкой.
"""

from __future__ import annotations

import json
import sys
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))

from agentpay.pricing import CostBreakdown  # noqa: E402
from agentpay.cli import demo_pricelist  # noqa: E402

LANDING = """<!doctype html><meta charset=utf-8><title>agentpay</title>
<style>
 body{background:#0b0d12;color:#e8ecf4;font:16px/1.6 system-ui,sans-serif;
      max-width:760px;margin:60px auto;padding:0 24px}
 code{background:#141821;padding:2px 6px;border-radius:5px;color:#f0a35e}
 pre{background:#141821;padding:12px;border-radius:9px;overflow:auto;font-size:14px}
 h1{font-size:26px;margin-bottom:4px} .s{color:#969eac} .box{background:#141821;
 border-radius:10px;padding:14px 18px;margin:14px 0}
</style>
<h1>agentpay</h1>
<p class=s>Price a call by what it actually consumed, not by what someone guessed.</p>

<div class=box><b>The problem.</b> Per-call billing usually means a price table.
The table is a guess made once, and the real cost drifts away from it in
both directions — you either overcharge people or eat the loss yourself,
without noticing which.</div>

<div class=box><b>Here.</b> Send the components the call actually used.
The price is computed from a rate card, with the breakdown attached, so
both sides see the same arithmetic.</div>

<h2>Try it</h2>
<pre>curl -X POST http://HOST:PORT/quote \\
  -H 'Content-Type: application/json' \\
  -d '{"tokens": 1200, "cpu_seconds": 0.4, "bytes_out": 2048,
       "storage_writes": 3, "external_calls": 1}'</pre>

<p class=s>More: <code>GET /pricing</code> &middot; <code>GET /health</code></p>
<p class=s>Public repo: github.com/mrpkk/agentpay</p>
"""

FIELDS = ("tokens", "cpu_seconds", "bytes_out", "storage_writes",
          "external_calls")


def _breakdown(raw: dict) -> CostBreakdown:
    """Собирает расход из тела запроса. Неизвестные поля игнорируются.

    Молча пропущенное поле не должно выглядеть как нулевой расход: ответ
    содержит весь перечень компонентов, поэтому отсутствие видно.
    """
    kw = {}
    for f in FIELDS:
        v = raw.get(f)
        if v is None or v == "":
            continue
        try:
            kw[f] = Decimal(str(v))
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"{f}: ожидалось число, получено {v!r}") from exc
    return CostBreakdown(**kw)


class Handler(BaseHTTPRequestHandler):
    server_version = "agentpay"

    def _send(self, code: int, payload: dict | str) -> None:
        raw = payload.encode() if isinstance(payload, str) else json.dumps(
            payload, ensure_ascii=False, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type",
                         "text/html; charset=utf-8" if isinstance(payload, str)
                         else "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        try:
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            return {}

    def do_GET(self) -> None:  # noqa: N802
        u = urlparse(self.path)
        path = u.path.rstrip("/") or "/"
        if path.startswith("/agentpay"):
            path = path[len("/agentpay"):] or "/"
        pl = demo_pricelist()
        if path == "/":
            self._send(200, LANDING)
        elif path == "/health":
            self._send(200, {"status": "ok", "service": "agentpay"})
        elif path == "/pricing":
            self._send(200, {
                "currency": pl.currency,
                "target_margin": str(pl.target_margin),
                "verified": pl.rates.is_verified,
                # Сериализуем по фактическим полям PriceItem: у модели
                # `usage` — разложение себестоимости, а полей `rate`/`unit`
                # не существует. Старая версия читала их и падала
                # AttributeError, а покупатель видел 502 на /pricing.
                "items": [
                    {
                        "name": i.name,
                        "description": i.description,
                        "cost": str(pl.cost_of(i.name)),
                        "price": str(pl.price_of(i.name)),
                        "margin": str(pl.margin_of(i.name)),
                        "usage": {
                            "tokens": str(i.usage.tokens),
                            "cpu_seconds": str(i.usage.cpu_seconds),
                            "bytes_out": str(i.usage.bytes_out),
                            "external_calls": str(i.usage.external_calls),
                        },
                    }
                    for i in pl.items
                ],
            })
        else:
            self._send(404, {"error": "not found", "path": path})

    def do_POST(self) -> None:  # noqa: N802
        u = urlparse(self.path)
        path = u.path.rstrip("/") or "/"
        if path.startswith("/agentpay"):
            path = path[len("/agentpay"):] or "/"
        if path != "/quote":
            return self._send(404, {"error": "not found", "path": path})
        body = self._body()
        try:
            usage = _breakdown(body)
        except ValueError as exc:
            return self._send(400, {"error": str(exc)})
        pl = demo_pricelist()
        cost = pl.rates.cost_of(usage)  # расход считает RateCard, не список позиций
        self._send(200, {
            "currency": pl.currency,
            "breakdown": {
                "tokens": str(usage.tokens),
                "cpu_seconds": str(usage.cpu_seconds),
                "bytes_out": str(usage.bytes_out),
                "storage_writes": str(usage.storage_writes),
                "external_calls": str(usage.external_calls),
            },
            "cost": str(cost),
            "price": str(cost * (Decimal(1) + pl.target_margin)),
            "target_margin": str(pl.target_margin),
            "pricing_verified": pl.rates.is_verified,
        })

    def log_message(self, *_a) -> None:
        pass


def serve(host: str = "127.0.0.1", port: int = 8187) -> None:
    print(f"agentpay: http://{host}:{port}", flush=True)
    srv = ThreadingHTTPServer((host, port), Handler)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8187)
    a = ap.parse_args()
    serve(a.host, a.port)