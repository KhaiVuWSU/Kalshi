# Kalshi Edge Scanner

Headless Python system that scans Kalshi prediction markets for **structural
mispricings** (not outcome predictions), logs them as signals, and
paper-trades them against real orderbook data to measure whether the edge is
real after fees and slippage.

Two strategies:

- **Strategy A** — intra-Kalshi structural violations: nested-window
  monotonicity, bucket-sum violations, YES+NO complement arbs.
- **Strategy B** — cross-platform gaps: Kalshi vs Polymarket pricing of the
  same event (Polymarket is read-only and signal-only; paper trades take the
  Kalshi leg).

**The MVP never places a live order.** `live_trading: true` hits a stub that
raises `NotImplementedError` by design.

## Setup

Python 3.11+.

```bash
pip install httpx websockets pydantic PyYAML cryptography
pip install pytest pytest-asyncio        # dev/tests
cp .env.example .env                     # fill in (see below)
```

`.env`:

| var | what |
|---|---|
| `KALSHI_API_KEY_ID` | API key id — create it in the **demo** environment |
| `KALSHI_PRIVATE_KEY_PATH` | path to the RSA private key PEM for that key |
| `DISCORD_WEBHOOK_URL` | webhook for alerts/digests/heartbeats (optional) |

All tunables (thresholds, cadences, rate limits, fee rates) live in
`config.yaml` — nothing is hardcoded.

## Running

```bash
python -m src.main verify-auth    # signed request against demo (auth check)
python -m src.main sync           # one-shot universe sync into SQLite
python -m src.main scan-once      # one snapshot + scan pass
python -m src.main run            # the always-on scanner (VPS)
```

`run` starts a single asyncio process with these loops: universe sync (15m),
Polymarket matcher (60m), snapshot+scan (30s), settlement/mark-to-market
(30m), Discord heartbeat (6h), daily digest and weekly report. Everything is
idempotent and resumable — kill it any time; on restart it re-syncs and
persistence streaks start over (no duplicate signals, see tests).

Suggested systemd unit:

```ini
[Service]
WorkingDirectory=/opt/kalshi-scanner
ExecStart=/usr/bin/python3 -m src.main run
Restart=always
RestartSec=10
```

## Operator workflow

**Relationship candidates (Strategy A).** Nesting that can't be proven from
metadata, and bucket exhaustiveness (which Kalshi metadata never asserts),
are never guessed. They queue for review:

```bash
python -m src.main candidates                  # list pending
python -m src.main confirm-relationship 12     # enable scanning it
python -m src.main reject-relationship 13
```

**Market pairs (Strategy B).**

```bash
python -m src.main pairs --status provisional  # review auto-matches
```

Confirm a pair by adding it to `pairs_override.yaml` (this file always wins
over the automatic matcher):

```yaml
pairs:
  - kalshi_ticker: KXFED-26SEP
    poly_condition_id: "0xabc..."
    status: confirmed
    notes: "hand-verified 2026-08-24"
```

Provisional pairs may alert (clearly labeled) but are never paper-traded.

**Reports.**

```bash
python -m src.main report daily     # print the daily digest
python -m src.main report weekly    # write reports/weekly-YYYY-MM-DD.md
```

## Fees — verify before trusting P&L

`fees verify`: the fee model is
`ceil_to_cent(rate × contracts × P × (1−P))` per order, taker rate 0.07,
maker 0.0175, with per-series overrides in `config.yaml`
(`kalshi_fee_rate_overrides`). **Re-check the published schedule**
(<https://kalshi.com/docs/kalshi-fee-schedule.pdf>) periodically and after any
Kalshi fee announcement; update the overrides table accordingly. Rounding
behavior (ceil-per-order, not per-contract) is unit-tested in
`tests/test_fees.py`.

## Tests

```bash
python -m pytest -q
```

Covers: fee rounding edge cases; every violation detector against hand-built
fixture books including near-misses that must NOT signal; fill-through-depth
simulation; the persistence gate (flicker never qualifies); snapshot replay
(planted violations detected, clean sessions silent); kill/restart recovery
(no duplicates, no corrupted state); matcher normalization/verification and
the override file; request signing.

## Layout

```
src/
  clients/kalshi.py       REST + WS client, RSA-PSS auth, token bucket
  clients/polymarket.py   Gamma + CLOB, read-only
  book.py                 orderbook model, executable prices, depth walking
  fees.py                 fee model + net-edge math
  ingest/universe.py      market universe -> SQLite (idempotent)
  ingest/snapshots.py     orderbook snapshots for the tracked set
  strategies/correlated.py     Strategy A
  strategies/cross_platform.py Strategy B
  strategies/common.py    signal types + persistence gate
  matching/matcher.py     Kalshi <-> Polymarket pair matching
  paper/engine.py         simulated fills, positions, settlement, P&L
  paper/report.py         daily digest + weekly report
  alerts/discord.py       webhook alerts
  db.py / config.py / main.py
```

See `NOTES.md` for doc-vs-spec discrepancies and decisions that need your
eyes before/at first deploy.
