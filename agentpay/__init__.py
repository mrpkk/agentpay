"""agentpay — rail-agnostic ядро цены и авторизации вызовов.

Модуль сознательно не знает ни про одну платёжную сеть: цена живёт в
PriceList, а расчётная сеть подключается адаптером. Это позволяет менять
рельс (x402 ↔ мандат AP2 ↔ MOCK) без правок ядра и продавать один и тот же
детерминированный вызов через любой канал.
"""

from __future__ import annotations

from .pricing import (
    PRICE_TO_VERIFY,
    TARGET_MARGIN,
    CostBreakdown,
    PriceItem,
    PriceList,
    RateCard,
)
from .quote import SCHEME_EXACT, SCHEME_UPTO, Quote, new_nonce, sign_quote
from .rails import (
    MandateRail,
    MockRail,
    Proof,
    Rail,
    Settlement,
    X402Rail,
    available,
    get,
    register,
)
from .verify import (
    MemoryNonceStore,
    NonceStore,
    Verdict,
    VerdictReason,
    check,
    consume,
    settle_amount,
)

__version__ = "0.1.0"

__all__ = [
    "__version__",
    "CostBreakdown",
    "MemoryNonceStore",
    "MandateRail",
    "MockRail",
    "NonceStore",
    "PRICE_TO_VERIFY",
    "PriceItem",
    "PriceList",
    "Proof",
    "Quote",
    "Rail",
    "RateCard",
    "SCHEME_EXACT",
    "SCHEME_UPTO",
    "Settlement",
    "TARGET_MARGIN",
    "Verdict",
    "VerdictReason",
    "X402Rail",
    "available",
    "check",
    "consume",
    "get",
    "new_nonce",
    "register",
    "settle_amount",
    "sign_quote",
]


from .x402 import (  # noqa: E402
    BASE_NETWORK,
    HEADER_CHALLENGE,
    USDC_BASE,
    USDC_DECIMALS,
    AssetNotAccepted,
    Challenge,
    ChallengeMalformed,
    NetworkNotAccepted,
    PayeeMismatch,
    PaymentRequirement,
    PriceEscalation,
    ResourceMismatch,
    SchemeNotSupported,
    Settlement,
    X402Error,
    AuthorizationMismatch,
    EIP712Domain,
    TokenDomainUnknown,
    build_exact_authorization,
    build_exact_payload,
    check_challenge,
    check_exact_payload,
    eip712_domain,
    new_payment_nonce,
    from_atoms,
    parse_challenge,
    parse_settlement,
    to_atoms,
)
