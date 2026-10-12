# agentpay

Rail-agnostic core for pricing and authorising AI-agent calls.

## The problem it solves

The price of an agent call is set by guesswork and welded to one payment rail.
The cost is never measured, the margin does not follow from a formula, and
switching rails (x402 ↔ AP2 ↔ mock) breaks the core.

This is the opposite: **the price is computed from what the call actually
consumed**, and the rail only verifies and transfers. A rail change touches
one file.

## Honest cost allocation

Cost breaks down into components that can each be measured:

| Component | Unit |
|---|---|
| `tokens` | per million |
| `cpu_seconds` | per CPU-hour |
| `bytes_out` | per megabyte |
| `storage_writes` | per write |
| `external_calls` | per call |

CPU time is measured as **process time**, not wall clock: waiting on the
network is not our cost and must not land in the customer's bill.

## Try it

```
curl -X POST https://rta.lat/agentpay/quote \
  -H 'Content-Type: application/json' \
  -d '{"tokens":1200,"cpu_seconds":0.4,"bytes_out":2048}'
```

The response carries the breakdown, the cost, the price and `pricing_verified`.

## `pricing_verified`

The rate card carries a source and a date. If either is missing, the API
reports `pricing_verified: false` rather than letting a made-up number pass as
a market price.

The shipped card is checked against OpenAI's published token pricing,
Hetzner's cloud pricing, AWS CloudFront egress and S3 storage. Prices move, so
the date is part of the answer.

## An honest note on the rate card

The card was originally a placeholder — and it also had a unit error: `$0.09
per megabyte` is `$90 per gigabyte`, while real egress costs about `$0.09/GB`.
That thousand-fold error went straight into the customer's bill. Both are
fixed, and a test now fails if the source or date are emptied again.

## Public repo

<https://github.com/mrpkk/agentpay>

Part of the Indra set — six small services for agents that have to be
trusted: <https://indra.rta.lat>
