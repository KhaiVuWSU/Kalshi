# NOTES — doc-vs-spec discrepancies and decisions made during the build

## Build-environment constraint (important)

This code was written in a sandbox whose egress proxy **blocks all Kalshi and
Polymarket hosts** (`api.elections.kalshi.com`, `demo-api.kalshi.co`,
`docs.kalshi.com`, `gamma-api.polymarket.com`, `clob.polymarket.com`,
`docs.polymarket.com` — all CONNECT 403). Consequences:

- API details below were verified against multiple current (2026) secondary
  sources and prior knowledge of the official docs, **not** against the live
  doc sites or live endpoints.
- No live request has been made yet. Milestone acceptance items that require
  live traffic (demo auth check, universe sync, snapshots flowing, hand-checked
  signals, 48h unattended run) must be executed on the deployment box.
  First-run checklist:
  1. `python -m src.main verify-auth` (demo key in `.env`) — signed request.
  2. `python -m src.main sync` — universe lands in SQLite; run twice, confirm
     idempotent (same market count, no duplicates).
  3. `python -m src.main scan-once` — snapshots + one scan pass.
  4. `curl "https://gamma-api.polymarket.com/markets?limit=1"` and
     `curl "https://clob.polymarket.com/book?token_id=<id>"` — read-only,
     no auth; confirm the response shapes match `clients/polymarket.py`.

## Kalshi API (verified via secondary sources, 2026)

- **Auth**: sign `"{timestamp_ms}{METHOD}{path}"` where path **includes** the
  `/trade-api/v2` prefix and **excludes** the query string; RSA-PSS with
  SHA-256, MGF1-SHA256, salt length = digest length (32). Headers:
  `KALSHI-ACCESS-KEY`, `KALSHI-ACCESS-SIGNATURE` (base64),
  `KALSHI-ACCESS-TIMESTAMP` (ms). Matches the spec.
- **Base URLs**: prod `https://api.elections.kalshi.com/trade-api/v2`, demo
  `https://demo-api.kalshi.co/trade-api/v2` — as in the spec. Both are in
  `config.yaml` in case they move.
- **Public market data** endpoints work unauthenticated on prod; we sign
  anyway when credentials are present (better rate limits).
- **Orderbook payload** lists resting *bids only* (`yes` and `no` arrays).
  Executable asks are derived: YES ask @ p ⇔ NO bid @ 100−p. All strategy
  math is built on this (see `src/book.py`).

## Fee schedule (checked against the July 2026 schedule via secondary sources)

- General taker formula `ceil_to_cent(0.07 × C × P × (1−P))` still holds;
  maker fee ≈ 0.0175 coefficient. Matches the spec's formula.
- Some series carry different rates (index markets have historically used a
  lower/different coefficient). These are NOT hardcoded — put them in
  `kalshi_fee_rate_overrides` in `config.yaml` after checking
  <https://kalshi.com/docs/kalshi-fee-schedule.pdf> (see README "fees verify").
- **Modeling decision**: for a fill that walks several price levels we sum the
  exact per-level fee and ceil **once** (single-order model). Ceil-per-level
  would only overstate fees slightly.

## Strategy A decisions (surfaced per spec §8)

- **Exhaustiveness is not in Kalshi metadata.** Events carry a
  `mutually_exclusive` flag but nothing asserts the buckets are exhaustive.
  Buy-all bucket arbs are therefore gated behind manual confirmation:
  candidates land in `relationship_candidates` (kind `exhaustive_event`) and
  are only scanned after `confirm-relationship <id>`. Sell-all arbs need only
  mutual exclusivity and run automatically.
- **Nesting auto-inference is deliberately narrow**: only same-event ladders
  with the *same* `strike_type` (greater/greater, less/less, between-contained)
  are treated as provable. Cross-event nesting (e.g. "by March" ⊆ "by June")
  is only ever a pending candidate — the workflow is
  `candidates` → `confirm-relationship <id>` (see README). If candidate volume
  is unmanageable at scale, that's the spec §8.1 conversation.

## Polymarket

- Gamma `/markets?active=true&closed=false` (offset pagination) for metadata;
  CLOB `/book?token_id=` for books. **No auth required for reads** as of the
  sources checked; re-verify current access terms on deploy (spec §8.2).
- `clobTokenIds` is ordered like the market's `outcomes` array — the client
  refuses any market whose outcomes aren't an unambiguous Yes/No pair rather
  than guessing token order (a wrong guess would invert every price).
- CLOB `/book` has a known intermittent staleness issue (stale 0.99/0.01
  books have been reported). The cross-platform slippage buffer and the
  persistence gate are the mitigations; if it recurs, compare against
  `/price` before trusting a signal.

## Matching / pairs

- The auto-confirm bar ("resolution criteria and date match exactly") is met
  almost never across platforms in practice — resolution prose differs even
  for identical events. Expect most confirmed pairs to come from
  `pairs_override.yaml` after human review of provisional pairs. If <50
  confirmed pairs genuinely exist, M3 reporting will say so with the real
  number (spec explicitly allows this).

## Misc

- Market links in alerts use `https://kalshi.com/markets/<ticker>`; Kalshi's
  canonical web URLs sometimes use series/event slugs — the ticker form
  redirects today but may need adjusting.
- Persistence streaks intentionally do **not** survive restarts: the process
  burns one scan-seq at startup, so a signal must re-earn its
  `persistence_scans` streak (we can't prove it persisted while we were down).
- WebSocket transport (`snapshot_transport: "ws"`) is implemented in
  `clients/kalshi.py` but REST polling is the default; the WS message schema
  (`orderbook_snapshot`/`orderbook_delta`) should be re-verified against docs
  before flipping the flag.
