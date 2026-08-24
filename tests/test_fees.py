from decimal import Decimal

from src.config import Config
from src.fees import (FeeSchedule, edge_result, fee_cents, fee_cents_for_fill,
                      qualifies)


class TestFeeFormula:
    def test_exact_no_rounding_needed(self):
        # 100 contracts @ 50c: 0.07 * 100 * 0.5 * 0.5 = $1.75 exactly
        assert fee_cents(100, 50) == 175

    def test_ceil_per_order_not_per_contract(self):
        # 10 @ 50c: exact fee is 17.5c -> ceil once to 18c.
        # Ceil-per-contract would be ceil(1.75)=2 x10 = 20c. This distinction
        # matters at small size and must not regress.
        assert fee_cents(10, 50) == 18
        assert fee_cents(10, 50) != 20

    def test_single_contract_rounds_up(self):
        # 1 @ 50c: 1.75c -> 2c
        assert fee_cents(1, 50) == 2

    def test_extreme_price_small(self):
        # 1 @ 1c: 0.07 * 0.01 * 0.99 = 0.0693c -> 1c
        assert fee_cents(1, 1) == 1
        assert fee_cents(1000, 1) == 70  # 69.3 -> 70

    def test_symmetry(self):
        assert fee_cents(37, 30) == fee_cents(37, 70)

    def test_boundary_prices_free(self):
        assert fee_cents(100, 0) == 0
        assert fee_cents(100, 100) == 0

    def test_zero_contracts(self):
        assert fee_cents(0, 50) == 0

    def test_no_float_noise_at_ceil_boundary(self):
        # 0.07 * 10 * 0.5 * 0.5 = 0.175 exactly; float would give 0.17500000000000002
        assert fee_cents(10, 50, 0.07) == 18
        # An exact-integer case must NOT round up an epsilon: 0.07*100*0.3*0.7=1.47
        assert fee_cents(100, 30) == 147

    def test_maker_rate(self):
        # 0.0175 * 100 * 0.25 = 43.75c -> 44
        assert fee_cents(100, 50, Decimal("0.0175")) == 44


class TestFillFees:
    def test_matches_single_level(self):
        assert fee_cents_for_fill([(50, 10)]) == fee_cents(10, 50)

    def test_multi_level_ceils_once(self):
        # exact = 0.07*(5*2500 + 5*2499)/100 = 17.4965 -> 18
        assert fee_cents_for_fill([(50, 5), (51, 5)]) == 18

    def test_skips_degenerate_levels(self):
        assert fee_cents_for_fill([(0, 10), (100, 10), (50, 0)]) == 0


class TestSchedule:
    def test_series_override(self):
        cfg = Config(kalshi_fee_rate_overrides={"KXINX": 0.035})
        sched = FeeSchedule.from_config(cfg)
        assert sched.fee_cents(100, 50, "KXINX") == 88   # 87.5 -> 88
        assert sched.fee_cents(100, 50, "OTHER") == 175
        assert sched.fee_cents(100, 50, "KXINX", maker=True) == 44


class TestEdgeMath:
    def test_edge_result(self):
        e = edge_result(100, 10000, 9000, 441)
        assert e.net_edge_cents == 559
        assert abs(e.net_edge_usd - 5.59) < 1e-9
        assert abs(e.net_edge_pct - 559 / 9441 * 100) < 1e-9
        assert abs(e.per_contract_net_cents - 5.59) < 1e-9

    def test_qualifies_requires_both_thresholds(self):
        good = edge_result(100, 10000, 9000, 441)     # 5.9%, $5.59
        assert qualifies(good, 2.0, 1.0)
        assert not qualifies(good, 2.0, 6.0)          # fails usd
        assert not qualifies(good, 7.0, 1.0)          # fails pct
        assert not qualifies(edge_result(0, 0, 0, 0), 0.0, 0.0)
