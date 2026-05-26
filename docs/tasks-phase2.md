# Tasks: PolyMarket Market Making (Phase 2)

> Companion to `docs/spec-phase2.md` and `docs/plan-phase2.md`. Ordered by dependency.
> Each task: ≤ ~5 files, single focused session, explicit acceptance + verification.
> Convention: write tests first for pure-logic tasks (TDD). `pytest` + `ruff` green before moving on.
> Task IDs are P-prefixed (P1, P2, …) to avoid colliding with Phase 1's T-series.

---

## Spine A — foundation & pure quoting logic

- [ ] **P1 — Config + models extensions**
  - Acceptance: `MarketMakingConfig` added to `common/config.py` (`sigma`, `base_spread`,
    `k_gamma`, `k_inv`, `target_inventory=0`, `max_inventory`, `quote_size`,
    `min_time_to_resolution`, `gamma_ceiling`, `tick_size`), wired as `mm:` on `Config` with
    `Decimal` coercion + validation; `Market` gains a resolution/end `datetime` field;
    new frozen value objects `Quote`, `MakerOrder`, `InventoryState` in `common/models.py`.
  - Verify: `pytest tests/common/test_config.py tests/common/test_models.py` — defaults,
    `Decimal` coercion, bad-value rejection, model invariants (quotes don't cross, money is Decimal).
  - Files: `common/config.py`, `common/models.py`, `tests/common/test_config.py`,
    `tests/common/test_models.py`, (update `config.example.toml`).

- [ ] **P2 — `phase2/pricing.py`** *(critical, TDD)*
  - Acceptance: pure stdlib-`math` BS-binary helpers: `logit(p)`/`logistic(x)` round-trip,
    `gamma_proxy(p, T)` (→0 as `T→∞`, →`gamma_ceiling`-shaped peak as `T→0` with `p` mid-range,
    symmetric about `p=0.5`), `delta(p, T)`; all take/return `float`, callers quantize.
  - Verify: `pytest tests/phase2/test_pricing.py` — round-trip identity within tol; gamma limits
    and symmetry; monotonicity of gamma as `T` shrinks at fixed mid-range `p`.
  - Files: `src/polymarket_bot/phase2_market_making/pricing.py`, `tests/phase2/test_pricing.py`.

- [ ] **P3 — `phase2/volatility.py`** *(TDD)*
  - Acceptance: `sigma(config, history=None)` returns the configured log-odds vol, or a
    rolling-window estimate (stdev of log-odds increments) when estimation is enabled and
    history is sufficient; falls back to config (with a floor) on thin history.
  - Verify: `pytest tests/phase2/test_volatility.py` — config passthrough; estimate on a
    synthetic series matches hand-computed; fallback + floor on short/empty history.
  - Files: `src/polymarket_bot/phase2_market_making/volatility.py`, `tests/phase2/test_volatility.py`.

- [ ] **P4 — `phase2/inventory.py`** *(critical, TDD)*
  - Acceptance: `InventoryState` updates from fills (signed YES shares); `skew(q, config)`;
    pure predicates `at_inventory_cap(q)`, `resolution_stop(T)`, `gamma_stop(gamma)`; helper
    to reduce per-side size as `|q|` nears the cap.
  - Verify: `pytest tests/phase2/test_inventory.py` — cap never breached, single-sided forcing
    near cap, stops fire exactly at thresholds, fill accounting signs correct.
  - Files: `src/polymarket_bot/phase2_market_making/inventory.py`, `tests/phase2/test_inventory.py`.

- [ ] **P5 — `phase2/strategy.py`** *(critical, TDD)*
  - Acceptance: `build_quotes(book, T, sigma, inventory, config) -> Quote | None`: reservation
    price `r_p = p* − k_inv·q`, half-spread `δ = base_spread + k_gamma·Γ(p,T)·σ`, YES bid/ask
    `r_p ∓ δ` clamped to (0,1) and the tick grid, size reduced near the cap; returns `None`
    when a resolution/gamma/inventory stop says pull quotes. All prices `Decimal` on the grid.
  - Verify: `pytest tests/phase2/test_strategy.py` — quotes straddle `r_p`; skew moves centre
    against inventory; spread widens monotonically as `T` shrinks; never crosses, never leaves
    (0,1), always on grid; `None` when each stop trips.
  - Files: `src/polymarket_bot/phase2_market_making/strategy.py`, `tests/phase2/test_strategy.py`.
  - **GATE:** spec criteria 2–3 demonstrable with zero I/O before any network/executor code.

## Branch B — execution & I/O (parallel with P2–P5 once P1 done; all mocked in default tests)

- [ ] **P6 — Maker executor (`common/execution` maker path)**
  - Acceptance: extend the executor surface for resting maker orders — `place(quote)`,
    `cancel(order)`, `reconcile() -> fills` — distinct from Phase 1's taker `execute(Opportunity)`.
    `PaperMakerExecutor` simulates resting fills against the book and tracks inventory, fees,
    **rebates** (`rebate_rate`/`DEFAULT_REBATE`), and PnL.
  - Verify: `pytest tests/common/test_paper_maker_executor.py` — fill updates inventory + PnL;
    rebate credited; cancel removes a quote; PnL == spread − fees + rebates within tolerance.
  - Files: `common/execution/__init__.py`, `common/execution/maker_base.py`,
    `common/execution/paper_maker.py`, `tests/common/test_paper_maker_executor.py`.

- [ ] **P7 — Client extensions (`gamma` end date + `clob` maker orders)**
  - Acceptance: `gamma._to_market` surfaces the market end/resolution date (parsed `datetime`);
    `clob` client gains place/cancel maker orders + read open orders/fills, wrapping
    `py-clob-client`; default tests use mocks/fixtures.
  - Verify: `pytest tests/common/test_clients.py` (fixtures, incl. end-date parse); opt-in
    `pytest -m live` does one read-only real fetch confirming end date parses (manual confirm).
  - Files: `common/clients/gamma.py`, `common/clients/clob.py`, `tests/common/test_clients.py`.

- [ ] **P8 — `common/execution` live maker** *(double flag-guarded)*
  - Acceptance: real maker order place/cancel/reconcile only when both guards set; honors
    inventory + exposure caps; never invoked by default tests.
  - Verify: `pytest tests/common/test_live_maker_executor.py` with mocked clob client — guard
    refusal, place/cancel/reconcile paths, cap enforcement. No real orders.
  - Files: `common/execution/live_maker.py`, `tests/common/test_live_maker_executor.py`.

## Converge

- [ ] **P9 — `phase2/runner.py`**
  - Acceptance: scan loop discover/select → fetch book + compute `T` → `sigma` → `build_quotes`
    → place/refresh (cancel stale) → `reconcile` fills → update inventory/PnL → log; runner is
    authoritative on cumulative exposure + inventory across scans; one bad market doesn't kill
    the loop; all four stops enforced.
  - Verify: `pytest tests/phase2/test_runner.py` end-to-end on fakes (stops provably bind,
    inventory mean-reverts, exposure cap holds); then a manual paper run on real data, no wallet.
  - Files: `src/polymarket_bot/phase2_market_making/runner.py`, `tests/phase2/test_runner.py`.

- [ ] **P10 — Quote log + analysis script**
  - Acceptance: `phase2/quote_log.py` writes a rolling, capped JSONL of quotes/fills/inventory/PnL
    (money as strings); `scripts/analyze_mm_log.py` (stdlib) summarizes fills, captured spread,
    fees, rebates, net PnL, and inventory over time.
  - Verify: `pytest tests/phase2/test_quote_log.py` — records + trims at cap, money as strings;
    run the analyzer on a sample log.
  - Files: `src/polymarket_bot/phase2_market_making/quote_log.py`, `scripts/analyze_mm_log.py`,
    `tests/phase2/test_quote_log.py`.

- [ ] **P11 — `phase2/cli.py`**
  - Acceptance: argparse (`--mode {paper,live}`, `--once`, `--max-scans`, `--config`,
    `--i-understand-the-risks`); **reuse the Phase 1 `go_live_gate`** verbatim; default paper.
  - Verify: `pytest tests/phase2/test_cli.py` — parsing, live-guard refusal without flag/confirm;
    manual `python -m polymarket_bot.phase2_market_making.cli --once`.
  - Files: `src/polymarket_bot/phase2_market_making/cli.py`,
    `src/polymarket_bot/phase2_market_making/__main__.py`, `tests/phase2/test_cli.py`.

## Pre-live (human-gated, per Boundaries "Ask first")

- [ ] **P12 — Live readiness review**
  - Acceptance: re-verify rebate/fee table against live docs; dry-run the live maker path with a
    mocked client; confirm all stops; explicit human go-ahead recorded.
  - Verify: checklist in an ADR (`docs/adr/0002-mm-go-live.md`); human approval before real orders.
  - Files: `docs/adr/0002-mm-go-live.md`.

---

## Dependency summary

```
P1 → P2 → P3 → P4 → P5(GATE)
   └→ P6 → P7 → P8            (parallel branch, needs P1)
                  P5,P8 → P9 → P10 → P11 → P12
```
