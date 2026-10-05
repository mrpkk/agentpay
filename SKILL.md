---
name: agentpay
description: Use when pricing or authorizing paid API calls for an AI agent — computing price from cost and margin, issuing x402 quotes on Base (USDC), switching between x402/AP2/MOCK rails, or integrating a paywall that a mini-agent can use without an account or a card.
---

# agentpay — оплата вызовов без аккаунта и карты

## Когда применять

- Нужно **посчитать цену** вызова из себестоимости, а не назначить на глаз.
- Нужно **выставить котировку** (quote) и **авторизовать вызов** агента.
- Нужна **платёжная стена для микроагента**, который не будет регистрироваться.
- Нужно **переключить рельс** (x402 ↔ AP2 ↔ MOCK) без правок ядра.

## Ключевое правило

```
МАРЖА = (ЦЕНА − СЕБЕСТОИМОСТЬ) / ЦЕНА
ЦЕНА  = ceil(СЕБЕСТОИМОСТЬ / (1 − TARGET_MARGIN))
```

Делить на саму маржу нельзя: `/ 0.60` даёт маржу 0.40, а не 0.60.
Округление строго вверх → фактическая маржа никогда не падает ниже целевой.

## Использование

```bash
export AGENTPAY_SECRET=...
export AGENTPAY_SALT=vendor-01

python3 -m agentpay.cli price      # каталог + диагностика маржи
python3 -m agentpay.cli quote      # расчёт котировки
```

```bash
python3 -m pytest tests/ -q        # 160 passed
```

## Рельсы

| Рельс | Сеть | Ассет |
|---|---|---|
| `x402` | Base (eip155:8453) | USDC |
| `ap2` | любая | по мандату |
| `mock` | — | для тестов |

## Проектное состояние

- Тесты: 160 passed.
- Лицензия Apache-2.0, код открыт.
- Спонсор: USDC в сети Base (адрес в README).
- Чего пока нет: публичного HTTPS-эндпоинта (нужен домен — блокер владельца).
