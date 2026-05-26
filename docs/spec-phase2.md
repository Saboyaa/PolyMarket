# Spec: PolyMarket Market Making (Phase 2)

> Phase 1 = intra-market arbitrage (shipped). Phase 2 = market making on binary
> markets, priced with a Black–Scholes binary-option model. This document specs
> Phase 2 only; it reuses the Phase 1 `common/` layers verbatim.

## Objective

Provide two-sided liquidity on Polymarket binary markets — quote a YES bid and a
YES ask (equivalently a NO ask and NO bid, since `NO = 1 − YES`) around a model
fair value — to earn the bid/ask **spread** and Polymarket **maker rebates**,
while keeping **inventory** controlled so a moving or resolving market cannot run
us over.

The pricing and risk engine is **Black–Scholes for a cash-or-nothing binary
option**, because a YES share *is* one: it pays $1 if the event resolves true,
$0 otherwise. The observed YES price is the risk-neutral probability `p = N(d₂)`.
We work in price/probability space and use the binary's **Greeks** — delta for
directional inventory exposure, gamma for how violently fair value moves — to set
quote skew and spread width as a function of `p` and time-to-resolution `T`.

**Target user:** the same single operator as Phase 1 — running an educational
bot, paper-first, optionally live behind explicit guards. Not a multi-tenant or
production trading service.

### Non-goals (explicit)
- No cross-market or statistical-arb strategies (Phase 1 covered intra-market arb).
- No continuous delta-hedging in a separate underlying — there isn't one; the only
  hedge for YES inventory is selling YES / buying NO, i.e. inventory management.
- No HFT/latency optimization. Quote refresh is on the order of seconds, reusing
  the Phase 1 scan cadence.
- No automatic fund management, withdrawals, or wallet operations beyond placing
  and cancelling maker orders.

## Strategy — Black–Scholes binary market making

### Fair value
A cash-or-nothing binary call paying $1 is worth `V = e^{−rT} N(d₂)`. Over a
Polymarket horizon we take `r ≈ 0`, so **fair value = `N(d₂)` = the event
probability**, which the book quotes directly. We therefore do **not** price from
an external underlying `S`/strike `K`/vol `σ`; we read `p` from the book and run
the model backwards to get the Greeks.

### The diffusing variable: log-odds
`p` is bounded in [0,1], so geometric Brownian motion is the wrong process. We
model the **log-odds** `x = ln(p / (1 − p))` as the latent diffusion (unbounded,
well-behaved, optionally mean-reverting). `p = logistic(x)` maps back to [0,1].
`σ` is the volatility of `x`. This is the "underlying" BS is missing here.

### Greeks we actually use
- **Mid / fair value** `p*`: the model's view of fair probability (seed = book
  mid; later refinements may add drift/mean-reversion of `x`).
- **Gamma proxy** `Γ(p, T)`: a binary's gamma is ~0 far from resolution and
  **explodes** as `T → 0` with `p` mid-range (the smooth `N(d₂)` curve collapses
  to a step at the strike). This is the master risk dial.
- **Delta** of accumulated inventory: net directional exposure, used to skew quotes.

### Quote construction (per market, per scan)
Given book mid `p`, time-to-resolution `T`, estimated log-odds vol `σ`, and current
signed inventory `q` (shares of net YES, negative = net NO):

1. **Half-spread** `δ = base_spread + k_gamma · Γ(p, T) · σ`.
   Tight far from resolution; widens sharply as `T` shrinks / gamma rises. Floored
   at the tick size and at a configured minimum; capped so we still post.
2. **Inventory skew** `s = k_inv · q` (Avellaneda–Stoikov-lite): shift the quote
   centre against inventory so fills mean-revert `q` toward the target (default 0).
3. **Reservation price** `r_p = p* − s`.
4. **Quotes:** YES bid = `r_p − δ`, YES ask = `r_p + δ`, both clamped to (0,1) and
   to the tick grid. Size per side from config, reduced as `|q|` nears the
   inventory cap (post less on the side that grows inventory).

### Hard risk boundaries (jump risk)
BS assumes continuous paths, but resolution is a **jump to 0 or 1**. So:
- **Resolution stop:** stop quoting and flatten inventory once `T <` a configured
  `min_time_to_resolution`; never hold inventory through settlement.
- **Gamma stop:** if `Γ(p, T)` exceeds a ceiling, pull quotes (don't just widen).
- **Inventory cap:** hard `|q| ≤ max_inventory`; never post a quote that could
  breach it.
- All existing Phase 1 caps still bind (`max_total_exposure`, per-trade notional).

### Volatility estimate `σ`
`σ` (log-odds vol) drives the spread. It is **config-overridable with optional
data-driven estimation**, mirroring how Phase 1 fees are config-overridable over a
data table:
- Default: a configured `sigma` value the operator tunes.
- When enabled, estimate `σ` from a rolling window of recent mids (reuse / extend
  the Phase 1 observation log as the price-history source), and fall back to the
  configured value when history is insufficient.

### Profitability accounting
Realized PnL per round-trip = **spread captured − fees paid + rebates earned −
inventory mark-to-model change**. Rebates use the existing `rebate_rate(category)`
/ `DEFAULT_REBATE` in `common/fees.py`. On liquid markets the spread is ~1¢, so the
rebate is often the dominant term — it is a first-class line in the accounting, not
an afterthought.

## Tech Stack

Identical to Phase 1 — no new runtime dependencies expected beyond the stdlib
`math` (for `erf`/`log`/`exp` in the binary Greeks). Python 3.11+ (3.12 in use),
`uv`, `pydantic` config, `httpx`, `py-clob-client`, `pytest`, `ruff` (line 100).
Money is always `Decimal`; the BS math is done in `float` *internally* (it is
inherently approximate) but every quoted price and size crossing the order
boundary is quantized back to `Decimal` on the tick grid.

## Commands

```bash
# Paper-mode market-making run (default; no wallet, simulated fills)
uv run python -m polymarket_bot.phase2_market_making.cli --once
uv run python -m polymarket_bot.phase2_market_making.cli --max-scans 100

# Live (DOUBLE-GUARDED, same as Phase 1: --mode live AND the flag AND typed confirm)
uv run python -m polymarket_bot.phase2_market_making.cli --mode live --i-understand-the-risks

# Analyze the MM quote/fill log
uv run python scripts/analyze_mm_log.py [path]

# Tests / lint
uv run pytest -q                 # excludes the `live` marker by default
uv run ruff check .
```

## Project Structure

Phase 2 lives in its own package; everything shared stays in `common/`.

```
src/polymarket_bot/
  common/                       # REUSED unchanged where possible
    models.py                   # + maker-order / quote value objects if needed
    fees.py                     # rebate_rate / DEFAULT_REBATE already present
    risk.py                     # extend with inventory-aware caps
    config.py                   # + MarketMakingConfig section
    clients/                    # gamma (discovery + resolution date), clob (book + orders)
    execution/                  # + a maker executor (place/cancel/reconcile)
  phase1_arbitrage/             # untouched
  phase2_market_making/
    pricing.py                  # BS binary: N(d2), gamma proxy, log-odds helpers
    volatility.py               # sigma: config value + rolling-window estimator
    strategy.py                 # quote construction (spread, skew, reservation price)
    inventory.py                # signed inventory tracking + caps + skew
    runner.py                   # scan loop: quote -> place/refresh -> reconcile fills
    cli.py                      # argparse + go-live gate (reuse Phase 1 gate)
    quote_log.py                # rolling JSONL of quotes/fills/inventory/PnL
```

The Gamma client must expose each market's **resolution / end date** so `T` is
computable; add it to the `Market` model if not already surfaced.

## Code Style

Match Phase 1 exactly:
- Frozen dataclasses for value objects (`Quote`, `MakerOrder`, `InventoryState`).
- `Decimal` for all money/size at API boundaries; `float` only inside `pricing.py`,
  quantized back before use. Never compare or store money as `float`.
- Pydantic for config with `Decimal` coercion validators (copy the Phase 1 pattern).
- One fee/rebate function path shared across paper and live (no duplicated math).
- Pure functions for the model: `pricing.py` and `strategy.py` take inputs and
  return quotes with no I/O, so they are trivially unit-testable.

## Testing Strategy

Match Phase 1: fast, deterministic, no network by default; `live` marker opt-in.
- **`pricing.py`:** unit-test `N(d₂)` against known values; assert the gamma proxy
  → 0 as `T → ∞` and → its ceiling as `T → 0` with `p` mid-range; log-odds round-trip
  `logistic(logit(p)) == p`.
- **`strategy.py`:** quotes straddle the reservation price; skew moves the centre
  *against* inventory; spread widens monotonically as `T` shrinks; quotes never
  cross, never leave (0,1), always on the tick grid.
- **`inventory.py`:** caps are never breached; the cap forces single-sided quoting;
  the resolution/gamma stops pull quotes.
- **`runner.py`:** fakes for market/book/order sources (as in Phase 1); a fill
  updates inventory and PnL; cumulative exposure respects `max_total_exposure`;
  one bad market doesn't kill the loop.
- **`quote_log.py`:** records quotes/fills with money as strings; rolling cap trims.
- **PnL invariant:** in a paper scenario with a known fill sequence, realized PnL ==
  spread − fees + rebates within a tolerance.

## Boundaries

**Always:**
- Paper-first. Default `mode = "paper"`, no wallet, simulated fills.
- Quantize every quote price/size to the tick grid as `Decimal` before sending.
- Enforce inventory, resolution, gamma, and exposure stops on *every* scan.
- Keep BS math in `float` internal-only; cross boundaries in `Decimal`.

**Ask first / guarded:**
- Live trading is DOUBLE-GUARDED exactly as Phase 1: `mode == "live"` **and**
  `--i-understand-the-risks` **and** a typed "I UNDERSTAND" confirmation. Reuse the
  Phase 1 `go_live_gate`.
- Any change to the rebate/fee table or the model's risk ceilings.

**Never:**
- Never log or commit the wallet private key (`POLYMARKET_WALLET_KEY`) or CLOB API
  secret. CLOB creds only in the git-ignored `.clob_creds.json` (0600).
- Never hold inventory through resolution.
- Never post a quote that could breach the inventory or exposure cap.
- Never store money as `float`.

## Success Criteria

1. Paper run quotes both sides on selected markets, simulates fills, and tracks
   signed inventory + PnL (spread, fees, rebates) per market and in aggregate.
2. Spread/skew demonstrably respond to `T`, gamma, and inventory per the model.
3. All four stops (inventory, resolution, gamma, exposure) provably bind in tests.
4. Live mode is reachable only through the double guard, reusing Phase 1's gate.
5. `scripts/analyze_mm_log.py` summarizes quotes/fills/inventory/PnL from the log.
6. `uv run pytest -q` green; `ruff check .` clean.

## Sources
- Wilmott, *Paul Wilmott Introduces Quantitative Finance* — Black–Scholes &
  binary (cash-or-nothing) options; the Greeks.
- Avellaneda & Stoikov, "High-frequency trading in a limit order book" — the
  inventory-skew / reservation-price formulation (used in its simplified form).
- Polymarket Maker Rebates — https://docs.polymarket.com/developers/market-makers/maker-rebates-program

## Open Questions
- Exact functional form of the gamma proxy `Γ(p, T)` and the constants
  `k_gamma`, `k_inv` — to be pinned during planning with a small calibration.
- Whether to seed the fair value `p*` from book mid only, or add a drift /
  mean-reversion term on the log-odds in the first cut (default: book mid only).
- Whether to surface resolution date by extending `Market` or via a separate
  Gamma lookup (default: extend `Market`).
