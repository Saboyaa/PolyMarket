# Implementation Plan: PolyMarket Market Making (Phase 2)

> Companion to `docs/spec-phase2.md`. Status: **DRAFT — awaiting human review** before tasks/code.
> Scope: Phase 2 (Black–Scholes binary market making), paper-first, live behind the
> Phase 1 double-guard. Reuses the `common/` layers; adds the `phase2_market_making/` package.

## 1. Component Map & Dependencies

```
                    ┌─────────────────┐
                    │ common/config   │  + MarketMakingConfig (mm: {...})
                    └────────┬────────┘
                             │
        ┌────────────────────┼─────────────────────┐
        ▼                    ▼                      ▼
┌───────────────┐   ┌────────────────┐     ┌────────────────┐
│ common/models │   │ common/fees    │     │ common/clients │
│ + Quote,      │   │ rebate_rate()  │     │  gamma: +end    │
│   MakerOrder, │   │ DEFAULT_REBATE │     │  date; clob:    │
│   resolution  │   │ (already there)│     │  place/cancel   │
└──────┬────────┘   └───────┬────────┘     └───────┬────────┘
       │                    │                      │
       ▼                    │                      │
┌──────────────────┐        │                      │
│ phase2/pricing   │ (pure) │                      │
│  N(d2), gamma,   │        │                      │
│  log-odds        │        │                      │
└───────┬──────────┘        │                      │
        ▼                    │                      │
┌──────────────────┐        │                      │
│ phase2/volatility│ (pure +│ rolling history)     │
│  sigma estimate  │        │                      │
└───────┬──────────┘        │                      │
        ▼          ┌─────────────────┐              │
┌──────────────────┤ phase2/inventory│ (pure caps + skew)
│ phase2/strategy  │ signed q, stops │              │
│  quote build     └────────┬────────┘              │
└───────┬──────────┘        │                       │
        │           ┌────────────────┐              │
        │           │ common/execution│ + maker     │
        │           │ base→paper,live │ executor    │
        │           └───────┬────────┘              │
        ▼                   ▼                        ▼
        ┌─────────────────────────────────────────────┐
        │ phase2_market_making/runner                 │
        │  quote → place/refresh → reconcile → log     │
        └──────────────────────┬──────────────────────┘
                               ▼
                       ┌───────────────┐
                       │ phase2 CLI     │ (reuse go_live_gate)
                       └───────────────┘
```

**Pure / no-I/O (test first, full coverage):** `pricing`, `volatility` (estimator math),
`strategy`, `inventory`, the maker `paper` executor.
**I/O / external (mock in tests):** gamma/clob client extensions, the maker `live` executor.

## 2. Build Order

Bottom-up; every layer tested before the next builds on it.

1. **Config + models** — add `MarketMakingConfig` (sigma, base_spread, k_gamma, k_inv,
   target_inventory, max_inventory, quote_size, min_time_to_resolution, gamma_ceiling,
   tick_size) to `common/config.py`; add `Quote`, `MakerOrder`, `InventoryState` value
   objects and a **resolution/end date** field on `Market`. Tests: parsing, defaults,
   `Decimal` coercion, model invariants.
2. **`phase2/pricing.py`** — pure BS binary helpers: `logit`/`logistic` round-trip,
   `n_d2(p)` (here fair value = `p`, so this layer mainly provides the **gamma proxy**
   `Γ(p, T)` and `delta`), implemented with stdlib `math`. Tests: round-trip identity;
   `Γ → 0` as `T → ∞`; `Γ → ceiling` as `T → 0` with `p` mid-range; symmetry about `p=0.5`.
3. **`phase2/volatility.py`** — `sigma()` returns the configured value, or a rolling-window
   estimate of log-odds vol from recent mids when enabled, falling back when history is
   thin. Tests: config passthrough, estimate on a synthetic series, fallback on short history.
4. **`phase2/inventory.py`** — `InventoryState` updates from fills; `skew(q)`, the
   inventory cap, and the resolution/gamma stops as pure predicates. Tests: cap never
   breached, single-sided forcing near the cap, stops fire on `T`/gamma thresholds.
5. **`phase2/strategy.py`** — `build_quotes(book, T, sigma, inventory, config) -> Quote|None`:
   reservation price, half-spread from gamma, skew from inventory, tick-grid clamping,
   size reduction near the cap; returns `None` when a stop says pull quotes. Tests: quotes
   straddle reservation price; skew moves centre against inventory; spread widens monotonically
   as `T` shrinks; never crosses, never leaves (0,1), always on the grid.
6. **`common/execution` maker path** — extend the executor interface for **place / cancel /
   reconcile** of resting maker orders (distinct from the Phase 1 taker `execute(Opportunity)`).
   `paper` simulates resting fills against the book; tracks inventory, fees, **rebates**, PnL.
   Tests: a fill updates inventory + PnL; rebate applied via `rebate_rate`; cancel removes a quote.
7. **`common/clients` extensions** — `gamma`: surface the market end/resolution date (extend
   `_to_market`). `clob`: place/cancel maker orders and read open orders / fills, wrapping
   `py-clob-client`. Tests use mocks/fixtures; live calls only under the opt-in `-m live` marker.
8. **`common/execution/live.py` maker** — real maker order place/cancel/reconcile, **double
   flag-guarded**, honoring inventory + exposure caps. Tests with a mocked clob client (no real orders).
9. **`phase2/runner.py`** — scan loop: discover/select → fetch book + `T` → `sigma` → build
   quotes → place/refresh (cancel stale) → reconcile fills → update inventory/PnL → log →
   enforce all stops. Authoritative cumulative-exposure + inventory tracking across scans
   (mirrors Phase 1 runner owning exposure). Tests with fakes end-to-end; stops provably bind.
10. **`phase2/quote_log.py` + `scripts/analyze_mm_log.py`** — rolling JSONL of
    quotes/fills/inventory/PnL (money as strings, capped); stdlib analysis summary
    (fills, captured spread, fees, rebates, net PnL, inventory over time). Tests: record/trim.
11. **`phase2/cli.py`** — argparse (`--mode`, `--once`, `--max-scans`, `--i-understand-the-risks`,
    `--config`); **reuse the Phase 1 `go_live_gate`** verbatim. Smoke tests on parsing + mode guard.

## 3. Parallelizable vs. Sequential

- **Sequential spine:** 1 → 2 → 3 → 4 → 5 (config/models → pricing → vol → inventory → strategy).
- **Parallel branch:** 6 → 7 → 8 (maker executor + client extensions + live maker) can proceed
  alongside 2–5 once step 1 (config/models) exists, since they don't depend on the quoting math.
- **Converge:** 9 (runner) needs both branches; 10 (logging/analysis) hangs off the runner;
  11 (CLI) last.

## 4. Risks & Mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| **Resolution jump** strands inventory at 0/1 | Large directional loss | Hard resolution stop: flatten + stop quoting before `min_time_to_resolution`; never hold through settlement. |
| **Gamma explosion near expiry** picks off resting quotes | Adverse selection | Spread widens with `Γ(p,T)`; gamma-ceiling stop *pulls* quotes (not just widens). |
| **Inventory runaway** in a trending market | Directional loss | Inventory-skew quotes toward target; hard `max_inventory` cap forces single-sided quoting; never post a quote that could breach it. |
| **σ misestimated** → spread too tight | Trades at a loss | σ config-overridable with a conservative floor; estimator falls back to config on thin history; review before live. |
| **Stale quotes** after book moves | Fills at bad prices | Cancel/replace stale quotes each scan; bound quote age to scan cadence. |
| **One-sided maker fill** | Inventory imbalance | Expected and managed by skew + caps; it's the core MM risk, not an error path. |
| **Rebate table drifts** (Polymarket changes %) | PnL overstated | Reuse effective-dated `fees.py` rebate table; config-overridable; re-verify before live. |
| **Accidental live trading** | Real money lost | Paper default; live needs `--mode live` **and** `--i-understand-the-risks` **and** typed confirm; default tests never exercise live. |
| **Float rounding on money** | Silent PnL error | BS math in `float` internal-only; quantize to `Decimal` tick grid at every boundary; tests assert exact quantized values. |

## 5. Verification Checkpoints

- **After step 5 (strategy):** pricing + inventory + strategy fully covered; hand-built books
  + `(p, T, σ, q)` inputs produce expected quotes, skew, and spread widening. Gate — quoting
  logic provably correct with zero I/O.
- **After step 6 (maker paper executor):** a simulated fill sequence yields PnL == spread −
  fees + rebates within tolerance. Gate before any network/auth code.
- **After step 7 (clients):** integration tests green against fixtures; one opt-in `-m live`
  read-only call (fetch a real book + confirm end date parses) confirmed manually.
- **After step 9 (runner):** full paper MM loop on live market data, no wallet access; inspect
  the quote log for sane quotes, inventory mean-reversion, and all four stops firing.
- **Before any live use:** re-verify rebate/fee table; dry-run the live maker path with a
  mocked client; explicit human go-ahead (per Boundaries "Ask first").

## 6. Definition of Done (Phase 2)

All spec Success Criteria (1–6) pass; `pytest` + `ruff` green; paper mode runs a clean
quoting loop on real market data without touching the wallet; inventory, resolution, gamma,
and exposure stops provably bind in tests; live maker path exists, is double-guarded, and is
verified only via mocks until explicit approval.
