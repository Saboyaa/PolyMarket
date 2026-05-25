# Tasks: PolyMarket Arbitrage Bot (Phase 1)

> Companion to `docs/spec.md` and `docs/plan.md`. Ordered by dependency.
> Each task: ≤ ~5 files, single focused session, explicit acceptance + verification.
> Convention: write tests first for pure-logic tasks (TDD). `pytest` + `ruff` green before moving on.

---

## Spine A — foundation & pure logic

- [ ] **T1 — Project skeleton**
  - Acceptance: installable package; `ruff` and `pytest` run (zero tests OK); `.env`/creds
    cache git-ignored; documented `config.example.toml`; empty `phase2_market_making/` stub.
  - Verify: `pip install -e ".[dev]"` succeeds; `ruff check .` and `pytest -q` exit 0.
  - Files: `pyproject.toml`, `.gitignore`, `config.example.toml`, `src/polymarket_bot/__init__.py`,
    `src/polymarket_bot/phase2_market_making/__init__.py`.

- [ ] **T2 — `common/config.py`**
  - Acceptance: pydantic config loads TOML + `.env`; exposes risk caps (defaults
    `max_total_exposure=10`, `max_trade_notional=5`), `min_net_edge_per_share=0.005`,
    `max_completion_slippage=0.02`, `scan_interval=5`, `mode`, fee overrides, market selector
    (id allowlist + category filter); invalid config raises clearly.
  - Verify: `pytest tests/common/test_config.py` — defaults, override, bad-value rejection.
  - Files: `src/polymarket_bot/common/config.py`, `tests/common/test_config.py`,
    (update `config.example.toml`).

- [ ] **T3 — `common/models.py`**
  - Acceptance: frozen dataclasses `Market`, `OrderBook` (ask/bid levels with price+size depth),
    `Order`, `Fill`, `Opportunity`; all money fields `Decimal`; basic invariants enforced.
  - Verify: `pytest tests/common/test_models.py` — construction, frozen, Decimal types.
  - Files: `src/polymarket_bot/common/models.py`, `tests/common/test_models.py`.

- [ ] **T4 — `common/fees.py`** *(critical, TDD)*
  - Acceptance: `feeRate × p × (1−p)` per-share formula; per-category rate table; effective-dated
    `FeeSchedule` selected by date; config override of the table; rebate table present (data);
    5-decimal rounding, min fee 0.00001.
  - Verify: `pytest tests/common/test_fees.py` — known formula values, symmetry (30¢==70¢),
    date-boundary selection (pre/post-March-2026), override applied, rounding/min-fee.
  - Files: `src/polymarket_bot/common/fees.py`, `tests/common/test_fees.py`.

- [ ] **T5 — `phase1_arbitrage/strategy.py`** *(critical, TDD)*
  - Acceptance: `find_intramarket_arb(book, fees)` computes net edge over **both-side depth**,
    sizes the `Opportunity`, returns `None` below `min_net_edge_per_share`.
  - Verify: `pytest tests/phase1_arbitrage/test_strategy.py` — positive edge, fees-eat-edge →
    None, asymmetric depth caps size, exactly-at-threshold boundary.
  - Files: `src/polymarket_bot/phase1_arbitrage/strategy.py`, `src/.../__init__.py`,
    `tests/phase1_arbitrage/test_strategy.py`.

- [ ] **T6 — `common/risk.py`** *(critical, TDD)*
  - Acceptance: `apply_caps(opportunity, open_exposure, config)` sizes down / skips against
    per-trade cap and remaining total-exposure headroom.
  - Verify: `pytest tests/common/test_risk.py` — under cap unchanged, over per-trade sized down,
    total-cap boundary skips, zero-headroom skips.
  - Files: `src/polymarket_bot/common/risk.py`, `tests/common/test_risk.py`.

- [ ] **T7 — `common/execution/base.py` + `paper.py`**
  - Acceptance: `Executor` interface; `PaperExecutor` does deterministic fills from a book,
    tracks simulated exposure + P&L, implements cross-the-spread completion when leg 2 moved.
  - Verify: `pytest tests/common/test_paper_executor.py` — fill accounting, exposure tracking,
    completion path against a book where the second leg has shifted.
  - Files: `src/polymarket_bot/common/execution/__init__.py`, `base.py`, `paper.py`,
    `tests/common/test_paper_executor.py`.
  - **GATE:** spec criteria 2–5 demonstrable in paper against fixture books before any network code.

## Branch B — external I/O (parallel with T5–T7 once T2–T3 done; all mocked in default tests)

- [ ] **T8 — `common/auth.py` + `auth derive` plumbing**
  - Acceptance: derive CLOB API creds from wallet key (L1→L2), cache to git-ignored location;
    no network in default tests.
  - Verify: `pytest tests/common/test_auth.py` with mocked client; manual `auth derive` later in T12.
  - Files: `src/polymarket_bot/common/auth.py`, `tests/common/test_auth.py`.

- [ ] **T9 — `common/clients/gamma.py` + `clob.py`**
  - Acceptance: market discovery (with selector applied), order-book fetch, order placement
    wrapping `py-clob-client`/`httpx`; backoff on rate limits; default tests use fixtures/mocks.
  - Verify: `pytest tests/common/test_clients.py` (fixtures); opt-in `pytest -m live` does one
    read-only real book fetch (manual confirm).
  - Files: `src/polymarket_bot/common/clients/__init__.py`, `gamma.py`, `clob.py`,
    `tests/common/test_clients.py`, fixtures.

- [ ] **T10 — `common/execution/live.py`** *(double flag-guarded)*
  - Acceptance: places real orders only when both guards set; cross-the-spread completion with
    `max_completion_slippage`; alert + halt if uncompletable; never invoked by default tests.
  - Verify: `pytest tests/common/test_live_executor.py` with mocked clob client — guard refusal,
    completion within slippage, halt path. No real orders.
  - Files: `src/polymarket_bot/common/execution/live.py`, `tests/common/test_live_executor.py`.

## Converge

- [ ] **T11 — `phase1_arbitrage/runner.py`**
  - Acceptance: loop discover → select → fetch books → detect → risk → execute → log;
    works with injected fake clients + paper executor.
  - Verify: `pytest tests/phase1_arbitrage/test_runner.py` end-to-end on fakes; then a manual
    paper `run` against real market data with no wallet access.
  - Files: `src/polymarket_bot/phase1_arbitrage/runner.py`, `tests/phase1_arbitrage/test_runner.py`.

- [ ] **T12 — `__main__.py` CLI**
  - Acceptance: `scan`, `run --mode {paper,live}`, `auth derive`; live requires
    `--i-understand-the-risks`; default mode is paper.
  - Verify: `pytest tests/test_cli.py` — arg parsing, live-guard refusal without flag; manual
    `python -m polymarket_bot scan` and `auth derive`.
  - Files: `src/polymarket_bot/__main__.py`, `tests/test_cli.py`.

## Pre-live (human-gated, per Boundaries "Ask first")

- [ ] **T13 — Live readiness review**
  - Acceptance: re-verify fee table + gas-free assumption against live docs; dry-run live path
    with mocked client; explicit human go-ahead recorded.
  - Verify: checklist in an ADR (`docs/adr/0001-go-live.md`); human approval before real orders.
  - Files: `docs/adr/0001-go-live.md`.

---

## Dependency summary

```
T1 → T2 → T3 → T4 → T5 → T6 → T7(GATE)
            └→ T8 → T9 → T10        (parallel branch)
                         T7,T10 → T11 → T12 → T13
```
