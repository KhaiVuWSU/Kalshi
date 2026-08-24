"""Kalshi fee model and net-edge math.

General trading fee (verify against the published schedule — see README):
    fee = ceil_to_cent(rate * contracts * P * (1 - P))
with P in dollars, taker rate 0.07 (maker 0.0175), and per-series overrides
loaded from config. Rounding is ceil-per-order (the whole order's fee is
rounded up to the next cent), not ceil-per-contract — this matters at small
size and is unit-tested explicitly.

All internal math uses integer cents and exact Decimal arithmetic so
float noise can never flip a ceil boundary.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal


@dataclass
class FeeSchedule:
    taker_rate: Decimal
    maker_rate: Decimal
    series_overrides: dict[str, Decimal]  # series ticker -> taker rate

    @classmethod
    def from_config(cls, cfg) -> "FeeSchedule":
        return cls(
            taker_rate=Decimal(str(cfg.kalshi_taker_fee_rate)),
            maker_rate=Decimal(str(cfg.kalshi_maker_fee_rate)),
            series_overrides={k: Decimal(str(v))
                              for k, v in (cfg.kalshi_fee_rate_overrides or {}).items()},
        )

    def rate_for(self, series_ticker: str | None, maker: bool = False) -> Decimal:
        if maker:
            return self.maker_rate
        if series_ticker and series_ticker in self.series_overrides:
            return self.series_overrides[series_ticker]
        return self.taker_rate

    def fee_cents(self, contracts: int, price_cents: int,
                  series_ticker: str | None = None, maker: bool = False) -> int:
        return fee_cents(contracts, price_cents,
                         self.rate_for(series_ticker, maker))


def fee_cents(contracts: int, price_cents: int,
              rate: Decimal | float = Decimal("0.07")) -> int:
    """Trading fee in cents for one order, ceil-per-order.

    fee_dollars = rate * C * (pc/100) * (1 - pc/100)
    fee_cents   = ceil(rate * C * pc * (100 - pc) / 100)
    """
    if contracts <= 0:
        return 0
    pc = int(price_cents)
    if pc <= 0 or pc >= 100:
        return 0
    r = Decimal(str(rate)) if not isinstance(rate, Decimal) else rate
    exact = r * Decimal(contracts) * Decimal(pc) * Decimal(100 - pc) / Decimal(100)
    return int(exact.to_integral_value(rounding=ROUND_CEILING))


def fee_cents_for_fill(levels: list[tuple[int, int]],
                       rate: Decimal | float = Decimal("0.07")) -> int:
    """Fee for a fill that walked multiple price levels.

    Kalshi charges per order at the executed price of each match; summing the
    exact (pre-ceil) fee across levels and ceiling once models the
    single-order case. Conservative alternative (ceil per level) would only
    overstate fees; we keep the single-order model and note it in NOTES.md.
    """
    r = Decimal(str(rate)) if not isinstance(rate, Decimal) else rate
    exact = Decimal(0)
    for price_cents, contracts in levels:
        pc = int(price_cents)
        if contracts <= 0 or pc <= 0 or pc >= 100:
            continue
        exact += r * Decimal(int(contracts)) * Decimal(pc) * Decimal(100 - pc) / Decimal(100)
    return int(exact.to_integral_value(rounding=ROUND_CEILING))


@dataclass
class EdgeResult:
    """Net edge of a candidate trade at executable depth."""
    contracts: int
    gross_edge_cents: float     # per contract, before fees
    fees_cents: int             # total fees across all legs
    net_edge_cents: float       # total net edge in cents (all contracts)
    net_edge_usd: float
    net_edge_pct: float         # net edge / capital at risk
    capital_at_risk_cents: int

    @property
    def per_contract_net_cents(self) -> float:
        return self.net_edge_cents / self.contracts if self.contracts else 0.0


def edge_result(contracts: int, gross_proceeds_cents: int, cost_cents: int,
                fees_cents: int) -> EdgeResult:
    net = gross_proceeds_cents - cost_cents - fees_cents
    capital = cost_cents + fees_cents
    return EdgeResult(
        contracts=contracts,
        gross_edge_cents=((gross_proceeds_cents - cost_cents) / contracts
                          if contracts else 0.0),
        fees_cents=fees_cents,
        net_edge_cents=float(net),
        net_edge_usd=net / 100.0,
        net_edge_pct=(net / capital * 100.0) if capital > 0 else 0.0,
        capital_at_risk_cents=capital,
    )


def qualifies(edge: EdgeResult, min_edge_pct: float, min_edge_usd: float) -> bool:
    return (edge.contracts > 0
            and edge.net_edge_pct >= min_edge_pct
            and edge.net_edge_usd >= min_edge_usd)
