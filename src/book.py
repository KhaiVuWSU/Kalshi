"""Orderbook model shared by ingestion, strategies, and the paper engine.

Kalshi's orderbook payload lists resting *bids* only: `yes` is bids to buy
YES, `no` is bids to buy NO (prices in cents, sizes in contracts). Because
YES and NO are complementary, an executable YES *ask* at price p exists for
every NO bid at 100 - p, and vice versa. All strategy math works on
executable (crossable) prices derived this way — never last trade, never
midpoint.
"""
from __future__ import annotations

from dataclasses import dataclass, field

Level = tuple[int, int]  # (price_cents, contracts)


def _sorted_bids(levels: list) -> list[Level]:
    return sorted(((int(p), int(q)) for p, q in levels if int(q) > 0),
                  key=lambda l: -l[0])


@dataclass
class OrderBook:
    """yes_bids / no_bids: [(price_cents, contracts)] — normalized best-first."""
    yes_bids: list[Level] = field(default_factory=list)
    no_bids: list[Level] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.yes_bids = _sorted_bids(self.yes_bids)
        self.no_bids = _sorted_bids(self.no_bids)

    @classmethod
    def from_kalshi(cls, payload: dict) -> "OrderBook":
        ob = payload.get("orderbook", payload) or {}
        return cls(yes_bids=ob.get("yes") or [], no_bids=ob.get("no") or [])

    # --- derived ask ladders (best/cheapest first) ---
    def asks(self, side: str) -> list[Level]:
        """Executable asks for `side`, derived from the other side's bids."""
        opp = self.no_bids if side == "yes" else self.yes_bids
        return [(100 - p, q) for p, q in opp]  # opp bids best-first => asks cheapest-first

    def bids(self, side: str) -> list[Level]:
        return self.yes_bids if side == "yes" else self.no_bids

    def best_ask(self, side: str) -> int | None:
        a = self.asks(side)
        return a[0][0] if a else None

    def best_bid(self, side: str) -> int | None:
        b = self.bids(side)
        return b[0][0] if b else None

    # --- depth walking ---
    def walk_buy(self, side: str, contracts: int) -> "Fill":
        """Fill a buy of `contracts` through the ask ladder (partial if thin)."""
        return _walk(self.asks(side), contracts)

    def walk_sell(self, side: str, contracts: int) -> "Fill":
        """Fill a sell of `contracts` through the bid ladder (partial if thin)."""
        return _walk(self.bids(side), contracts)

    def depth_contracts(self, side: str, action: str) -> int:
        ladder = self.asks(side) if action == "buy" else self.bids(side)
        return sum(q for _, q in ladder)

    def size_at_or_better(self, side: str, action: str, limit_price: int) -> int:
        """Contracts executable at prices at least as good as limit_price."""
        if action == "buy":
            return sum(q for p, q in self.asks(side) if p <= limit_price)
        return sum(q for p, q in self.bids(side) if p >= limit_price)


@dataclass
class Fill:
    contracts: int              # filled contracts (may be < requested)
    avg_price_cents: float      # size-weighted average
    cost_cents: int             # total notional at fill prices
    levels: list[Level]         # (price, contracts) actually consumed
    worst_price_cents: int | None


def _walk(ladder: list[Level], contracts: int) -> Fill:
    remaining = max(0, int(contracts))
    filled = 0
    cost = 0
    used: list[Level] = []
    worst: int | None = None
    for price, qty in ladder:
        if remaining <= 0:
            break
        take = min(qty, remaining)
        used.append((price, take))
        cost += price * take
        filled += take
        remaining -= take
        worst = price
    avg = (cost / filled) if filled else 0.0
    return Fill(contracts=filled, avg_price_cents=avg, cost_cents=cost,
                levels=used, worst_price_cents=worst)
