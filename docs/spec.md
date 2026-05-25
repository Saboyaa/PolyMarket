# Spec: PolyMarket Arbitrage Bot

> Status: **DRAFT — awaiting human review**. Do not begin implementation until approved.
> Phase 1 = intra-market arbitrage. Phase 2 = market making (out of scope for this spec, sketched only).

## Objective

Build a Python bot that detects and (eventually) executes **intra-market arbitrage** on
Polymarket binary markets: whenever the best ask for YES plus the best ask for NO,
**including all fees**, is less than the guaranteed $1.00 payout at resolution, buy both
sides to lock in risk-free profit.

The bot ships in two trading modes:
- **Paper mode (default):** simulate fills against the live order book; never touch the wallet.
- **Live mode (flag-guarded):** place real CLOB orders with the funded wallet.

**Users:** the operator (you) running a long-lived process on a Linux host.

**Context:** this is primarily an **educational project**. Capital is intentionally tiny —
a **$10 total cap**. Correctness, clarity, and safe-by-default behavior matter more than
throughput or squeezing edge. Real opportunities at this size will be rare; **paper mode is
where most of the value and learning lives.**

**Success looks like:** in paper mode, the bot continuously scans markets, correctly
identifies opportunities where `ask(YES) + ask(NO) + fees < $1`, and logs each as a
simulated locked-in profit with accurate post-fee math. Live mode places the two legs
atomically-as-possible and reconciles fills, never exceeding the configured per-trade cap.

### Phase 2 (sketch only — not specified here)
Market making: quote both sides, capture spread, manage inventory. Will reuse the
order-book, fee, execution, config, auth, and risk layers built as `common/` in Phase 1.

## Tech Stack

- **Python 3.11+**
- **`py-clob-client`** (official Polymarket CLOB client) for market data + order placement
- **`web3`** / `eth-account` for wallet handling and CLOB API-key derivation
- **`httpx`** for the Gamma/data REST API (market discovery)
- **`pydantic`** for typed config + market models
- **`pytest`** for tests
- Config via a TOML file + `.env` for secrets

> Assumption — confirm: Polymarket is on **Polygon**, USDC-settled, accessed via the CLOB.
> Wallet exists but **CLOB API credentials must be derived** from the wallet key (L1→L2 auth).

## Commands

```
Install:   pip install -e ".[dev]"          # or: uv pip install -e ".[dev]"
Run paper: python -m polymarket_bot run --mode paper
Run live:  python -m polymarket_bot run --mode live --i-understand-the-risks
Scan once: python -m polymarket_bot scan          # one-shot opportunity scan, no loop
Derive keys: python -m polymarket_bot auth derive  # create/show CLOB API creds from wallet
Test:      pytest -q
Test+cov:  pytest --cov=polymarket_bot --cov-report=term-missing
Lint:      ruff check . && ruff format --check .
```

> Live mode requires an explicit `--i-understand-the-risks` flag in addition to `--mode live`.

## Project Structure

Phase 1 and Phase 2 live in **separate packages**. Anything used by both goes in `common/`.

```
docs/                          → This spec, plan, tasks, ADRs
src/polymarket_bot/
  __init__.py
  __main__.py                  → CLI entrypoint (dispatches to a phase's runner)

  common/                      → Shared infrastructure (serves both phases)
    __init__.py
    config.py                  → Pydantic config models, loads TOML + .env
    auth.py                    → Derive/cache CLOB API creds from wallet key
    models.py                  → Market, OrderBook, Order, Fill value objects
    fees.py                    → Effective-dated fee schedule + post-fee math
    risk.py                    → Pre-trade checks (per-trade cap)
    clients/
      __init__.py
      clob.py                  → Thin wrapper over py-clob-client (orders, books)
      gamma.py                 → Market discovery via REST
    execution/
      __init__.py
      base.py                  → Executor interface
      paper.py                 → Simulated fills
      live.py                  → Real CLOB orders (flag-guarded)

  phase1_arbitrage/            → Intra-market YES+NO arbitrage
    __init__.py
    strategy.py                → find_intramarket_arb(book, fees) -> Opportunity | None
    runner.py                  → Scan loop: discover → detect → size → execute

  phase2_market_making/        → Phase 2 (placeholder; not implemented yet)
    __init__.py                → Empty stub; documented as future work

config.example.toml            → Documented sample config
tests/                         → Mirrors src/ layout (tests/common, tests/phase1_arbitrage)
```

> Rule: a module only moves into `common/` once it genuinely serves both phases. Phase-1-only
> logic (the arb detector, the arb runner) stays in `phase1_arbitrage/`.

## Code Style

- `ruff` for lint + format (line length 100). Full type hints. `pydantic` models for
  external data; plain `@dataclass(frozen=True)` for internal value objects.
- Money handled as **`Decimal`** (or integer micro-USDC), never float, to avoid rounding drift.
- Pure functions for all profit/fee math (easy to unit-test); side effects isolated to
  clients and executors.

```python
# common/models.py
from decimal import Decimal
from dataclasses import dataclass

@dataclass(frozen=True)
class Opportunity:
    market_id: str
    yes_ask: Decimal          # price to buy YES, in USDC (0..1)
    no_ask: Decimal           # price to buy NO,  in USDC (0..1)
    size: Decimal             # shares tradable at these asks
    gross_edge: Decimal       # 1 - (yes_ask + no_ask)
    net_edge: Decimal         # gross_edge after fees; must be > 0 to act

# phase1_arbitrage/strategy.py
def find_intramarket_arb(book: "OrderBook", fees: "FeeSchedule") -> Opportunity | None:
    """Return an Opportunity iff buying YES+NO nets positive edge after fees."""
    ...
```

## Fee Model (critical — fees change over time)

Researched current model (as of the **March 2026** change; sources at end of spec):

- **Taker fees only.** Maker (limit) orders are free and may earn rebates. Our arb buys at
  the ask = **taker**, so **both legs pay a taker fee**.
- **Dynamic per-share fee:** `fee = shares × feeRate × p × (1 − p)`, charged in USDC with
  5-decimal precision, min 0.00001. Peaks at `p = 0.50`, symmetric (30¢ trade == 70¢ trade).
- **Per-category `feeRate`** (current values — *keep these as data, easy to change*):

  | Category | feeRate | | Category | feeRate |
  |---|---|---|---|---|
  | Crypto | 0.07 | | Culture | 0.05 |
  | Economics | 0.05 | | Weather | 0.05 |
  | Sports | 0.03 | | Mentions | 0.04 |
  | Finance | 0.04 | | Tech | 0.04 |
  | Politics | 0.04 | | Other/General | 0.05 |
  | Geopolitics | 0 (fee-free) | | | |

**Arb implication:** for shares `C`, YES ask `a_y`, NO ask `a_n`, category rate `r`:
```
cost     = C × (a_y + a_n)
fees     = C × r × ( a_y(1 − a_y) + a_n(1 − a_n) )
net_edge = C × 1.0  −  cost  −  fees        # acts only when net_edge > min_margin
```

**Maker rebates** (for Phase 2 market making — captured now so the data table is complete):
Crypto 20%, all other fee-bearing categories 25%, Geopolitics none. Rebate =
`(your_fee_equivalent / total_fee_equivalent) × rebate_pool`, per-market, min $1 USDC payout.
Docs note the rebate % is "at the sole discretion of Polymarket and may change over time" —
another reason the table must be config-overridable and effective-dated.

Requirements for `common/fees.py`:
- **Centralized** — no fee constants scattered through strategy code; rates live as a data table.
- **Effective-dated:** a `FeeSchedule` is a list of `(effective_from, {category: feeRate})`
  entries; the active schedule is selected by date so backtest math stays correct across
  fee changes. The fee *formula* and the *rate table* are independently overridable, because
  Polymarket has changed both.
- **Config-overridable:** operator can pin/override the whole rate table in `config.toml`
  without a code change — this is the primary "fees change a lot" escape hatch.
- Applied in **one** function so net-edge math is identical across paper, live, and both phases.

> Rates and formula above are from public docs/help pages (April 2026); treat them as the
> seed of the effective-dated table, not as immutable. Re-verify before live trading.

## Testing Strategy

- **Framework:** `pytest`. Tests mirror `src/` (`tests/common/`, `tests/phase1_arbitrage/`).
- **Unit (priority):** fee math, net-edge calculation, opportunity detection against
  hand-built order books, effective-dated fee selection, risk cap enforcement. Target
  **near-100% coverage on `common/fees.py`, `common/risk.py`, `phase1_arbitrage/strategy.py`**
  — these are pure and critical.
- **Integration:** clients tested against recorded fixtures / mocked HTTP; **no live network
  calls in the default test run.** A separate opt-in `-m live` marker can hit the real API.
- **Paper executor** is itself a test harness: deterministic fills from a fixed book.
- Every detected-opportunity bug gets a regression test (Prove-It pattern).

## Boundaries

**Always:**
- Run `pytest` + `ruff` before any commit.
- Compute net edge after fees; only act when `net_edge > 0` by the configured min margin.
- Enforce **both** the per-trade size cap and the total-exposure cap before placing/simulating
  any order; read both from config.
- Keep secrets (wallet key, API creds) out of git — `.env` + `.gitignore`.

**Ask first:**
- Switching the default mode away from paper.
- Adding new dependencies beyond those listed in Tech Stack.
- Anything that places real orders / spends real USDC.
- Changing the fee model's structure.
- Promoting a Phase-1 module into `common/`.

**Never:**
- Commit the wallet private key or CLOB API secret.
- Place live orders without both `--mode live` and `--i-understand-the-risks`.
- Hardcode fee values inside strategy logic.
- Float arithmetic on money.

## Success Criteria

1. `python -m polymarket_bot auth derive` produces working CLOB API creds from the wallet.
2. `scan` lists current markets and flags any with `net_edge > min_margin`, showing the
   YES ask, NO ask, fees applied, and net edge — verified by unit tests on known books.
3. Paper `run` loops, logging simulated locked-in profit; never touches the wallet.
4. Fee math is provably correct via unit tests, including an effective-date boundary
   (pre- vs post-March-2026 schedule selection).
5. Per-trade **and** total-exposure caps are enforced (both config-driven, defaults $5 / $10):
   an opportunity exceeding the per-trade cap or remaining total headroom is sized down or
   skipped — covered by tests including the boundary where cumulative exposure hits the cap.
6. Live mode is reachable only behind both guard flags and is **not** exercised by default
   tests or by `run` without the explicit flags.
7. `common/` contains only modules used by both phases; Phase-1 arb logic stays in
   `phase1_arbitrage/`; `phase2_market_making/` is an empty documented stub.
8. Scan covers all active markets by default; a configured selector (id allowlist / category
   filter) restricts the set — covered by a test on the selector.
9. Live one-leg-fill handling crosses the spread to complete the pair within
   `max_completion_slippage`, else alerts and halts — covered by a paper/mocked test.

## Sources

- Polymarket Maker Rebates / fee formula — https://docs.polymarket.com/developers/market-makers/maker-rebates-program
- Trading Fees (Help Center) — https://help.polymarket.com/en/articles/13364478-trading-fees
- Fee change context (April 2026) — https://www.pokernews.com/prediction-markets/news/2026/04/polymarket-blunder-prompts-quick-u-turn-new-polymarket-fees-50947.htm

## Resolved Decisions

1. **Fees** — researched (see Fee Model). Modeled as an effective-dated, config-overridable
   per-category rate table feeding the dynamic `feeRate × p × (1−p)` formula. Re-verify before live.
2. **Market scope** — scan **all active binary markets by default**, with a config **selector**
   to restrict to a chosen subset: an explicit market/condition-id allowlist and/or category
   filter. No selector configured ⇒ scan everything. The selector also bounds rate-limiting.
3. **Execution (one-leg fill)** — in live mode, **cross the spread to complete the pair**:
   if the first leg fills but the second does not at the expected price, immediately take
   liquidity on the second leg to close the position, accepting reduced/zero edge to avoid
   holding a naked directional position. The completion cost is bounded by a configurable
   `max_completion_slippage`; if even crossing can't complete, alert + halt. The detector
   must therefore size against *available depth on both sides*, not just top-of-book.
4. **Risk caps & defaults — educational sizing.** All of these live in `config.toml` and are
   **freely changeable**; the values below are just defaults:
   - `max_total_exposure = 10` USDC — bot opens no new position once cumulative open exposure
     hits this. **Default $10, but fully configurable** (raise/lower in config, no code change).
   - `max_trade_notional = 5` USDC per opportunity (≤ half the total cap, so ≥2 positions fit).
   - `min_net_edge_per_share = 0.005` USDC (act only when post-fee edge ≥ 0.5¢/share).
   - `max_completion_slippage = 0.02` USDC/share (cap on crossing the spread to complete a pair).
   - `scan_interval = 5s`.

   `common/risk.py` enforces **both** the per-trade cap and the total-exposure cap before any
   order (paper or live); an opportunity exceeding remaining headroom is sized down or skipped.

## Open Questions

- Confirm the default numbers above (or adjust — all are config-editable regardless).
- Confirm there are no Polygon gas / relayer costs to model for CLOB orders (research
  indicates trading is gas-free for the taker; verify before live).
