# Implementation Plan: PolyMarket Arbitrage Bot (Phase 1)

> Companion to `docs/spec.md`. Status: **DRAFT — awaiting human review** before tasks/code.
> Scope: Phase 1 (intra-market YES+NO arbitrage), paper-first, live behind guard flags.

## 1. Component Map & Dependencies

```
                    ┌─────────────────┐
                    │ common/config   │  (no deps; pydantic + TOML/.env)
                    └────────┬────────┘
                             │
        ┌────────────────────┼─────────────────────┐
        ▼                    ▼                      ▼
┌───────────────┐   ┌────────────────┐     ┌────────────────┐
│ common/models │   │ common/fees    │     │ common/auth    │
│ (value types) │   │ (pure math)    │     │ (derive creds) │
└──────┬────────┘   └───────┬────────┘     └───────┬────────┘
       │                    │                      │
       │                    │                      ▼
       │                    │            ┌──────────────────┐
       │                    │            │ common/clients   │
       │                    │            │  clob, gamma     │
       │                    │            └────────┬─────────┘
       ▼                    ▼                     │
┌──────────────────────────────────┐             │
│ phase1_arbitrage/strategy        │             │
│  find_intramarket_arb()          │ (pure)      │
└──────────────┬───────────────────┘             │
               │            ┌────────────────┐    │
               │            │ common/risk    │    │
               │            │ (pure caps)    │    │
               │            └───────┬────────┘    │
               ▼                    ▼             ▼
        ┌────────────────────────────────────────────┐
        │ common/execution: base → paper, live        │
        └──────────────────────┬─────────────────────┘
                               ▼
                  ┌─────────────────────────┐
                  │ phase1_arbitrage/runner │  scan loop
                  └────────────┬────────────┘
                               ▼
                       ┌───────────────┐
                       │ __main__ CLI  │
                       └───────────────┘
```

**Pure / no-I/O (test first, full coverage):** `models`, `fees`, `strategy`, `risk`, `paper` executor.
**I/O / external (mock in tests):** `auth`, `clients/*`, `live` executor.

## 2. Build Order

Bottom-up so every layer has a tested foundation before the next is built.

1. **Project skeleton** — `pyproject.toml` (deps, ruff, pytest), package dirs incl. empty
   `phase2_market_making/` stub, `.gitignore` (`.env`, creds cache), `config.example.toml`.
2. **`common/config.py`** — typed config: risk caps, fee overrides, market selector,
   scan interval, mode. Loads TOML + `.env`. Tests: parsing, defaults ($10/$5), validation.
3. **`common/models.py`** — `Market`, `OrderBook` (bid/ask levels with depth), `Order`,
   `Fill`, `Opportunity`. Frozen dataclasses, `Decimal` money. Tests: construction/invariants.
4. **`common/fees.py`** — effective-dated `FeeSchedule`, per-category rate table, the
   `feeRate × p × (1−p)` formula, config override hook, rebate table (data only).
   Tests: formula values, symmetry, date-boundary selection, override, min-fee rounding.
5. **`phase1_arbitrage/strategy.py`** — `find_intramarket_arb(book, fees)`: compute net edge
   over **depth on both sides**, return sized `Opportunity` or `None`. Tests: positive/negative
   edge, fee-eats-edge, asymmetric depth limiting size, min-margin threshold.
6. **`common/risk.py`** — `apply_caps(opportunity, open_exposure, config)`: size-down/skip
   against per-trade and total-exposure caps. Tests: under cap, sized down, total-cap boundary.
7. **`common/execution/base.py` + `paper.py`** — executor interface; paper does deterministic
   fills from the book, tracks simulated exposure & P&L. Tests: fills, exposure accounting,
   the cross-the-spread completion path simulated against a book where leg 2 moved.
8. **`common/auth.py`** — derive/cache CLOB API creds from wallet key (L1→L2). Test against
   mocked client; never hits network in default run. `auth derive` CLI command.
9. **`common/clients/gamma.py` + `clob.py`** — market discovery + order book fetch + order
   placement, wrapping `py-clob-client`/`httpx`. Tests use recorded fixtures / mocks; live
   calls only under opt-in `-m live` marker.
10. **`common/execution/live.py`** — real orders, **double flag-guarded**, implements
    cross-the-spread completion with `max_completion_slippage`; alert+halt if uncompletable.
    Tests with mocked clob client (no real orders).
11. **`phase1_arbitrage/runner.py`** — wire discover → select → fetch books → detect → risk →
    execute → log loop. Tests with fake clients + paper executor end-to-end.
12. **`__main__.py` CLI** — `scan`, `run --mode`, `auth derive`, the `--i-understand-the-risks`
    guard. Smoke tests on arg parsing & mode guarding.

## 3. Parallelizable vs. Sequential

- **Sequential spine:** 1 → 2 → 3 → 4 → 5 → 6 → 7 (each builds on the last).
- **Parallel branch:** 8 → 9 → 10 (auth + clients + live executor) can proceed alongside
  5/6/7 once `config` and `models` (steps 2–3) exist, since they don't depend on the strategy.
- **Converge:** 11 (runner) needs both branches. 12 (CLI) last.

## 4. Risks & Mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| **One-leg fill** leaves naked position (live) | Directional loss | Cross-the-spread completion bounded by `max_completion_slippage`; alert+halt if uncompletable; size against both-side depth so completion liquidity is likely present. |
| **Fee model drifts** (Polymarket changes fees/rebates) | Wrong edge → bad trades | Effective-dated + config-overridable table; re-verify before any live run; unit-tested date boundary. |
| **Accidental live trading** | Real money lost | Paper default; live needs `--mode live` **and** `--i-understand-the-risks`; default tests never exercise live. |
| **Float rounding on money** | Silent edge miscalc | `Decimal` everywhere; lint/review rule; tests assert exact values. |
| **CLOB API auth / rate limits** | Bot stalls or errors | Cache derived creds; respect selector to bound request volume; backoff in clients. |
| **Thin liquidity at $10 scale** | Few/no real ops | Expected — educational; paper mode is primary; opportunities logged regardless. |
| **Gas/relayer cost unmodeled** | Edge overstated | Open question; verify gas-free before live, else add a cost term to `fees.py`. |

## 5. Verification Checkpoints

- **After step 4 (fees):** unit suite proves formula + date-boundary + override. Gate.
- **After step 6 (strategy+risk):** detector + caps fully covered; hand-built books produce
  expected opportunities and sizing. Gate — core logic provably correct.
- **After step 7 (paper):** `scan` and a paper `run` work end-to-end against fixture books;
  cross-spread completion simulated. Gate before touching any network/auth code.
- **After step 9 (clients):** integration tests green against fixtures; one opt-in `-m live`
  read-only call (fetch a real book) confirmed manually.
- **After step 11 (runner):** full paper loop on live market data, no wallet access; review
  logged opportunities for sanity vs. manual fee math.
- **Before any live use:** re-verify fee table + gas assumption; dry-run live path with
  mocked client; explicit human go-ahead (per Boundaries "Ask first").

## 6. Definition of Done (Phase 1)

All spec Success Criteria (1–9) pass; `pytest` + `ruff` green; paper mode runs a clean loop
on real market data without touching the wallet; live path exists, is flag-guarded, and is
verified only via mocks until explicit approval.
