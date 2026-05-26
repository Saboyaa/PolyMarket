# ADR 0002 — Phase 2 market-making go-live readiness

- **Status:** PROPOSED — NOT approved for live trading. Paper mode only until a human signs off below.
- **Date:** 2026-05-26
- **Context:** Phase 2 (Black–Scholes binary market making) is implemented and tested in
  paper mode (tasks P1–P11). This ADR is the human-gated checklist (task P12) that must be
  completed and signed before `--mode live --i-understand-the-risks` is ever used.

## Decision

Live market making is **disarmed by default** and stays disarmed until every item below is
verified and the sign-off line is filled in. The code enforces the double guard
(`mode == "live"` AND `--i-understand-the-risks`) plus the interactive typed "I UNDERSTAND"
confirmation (the reused `go_live_gate`), but those guards are the *floor*, not a substitute
for this review.

## Pre-live checklist

### Economics / model
- [ ] Re-verify the maker **rebate table** (`common/fees.py`, `rebate_rate` / `MARCH_2026_REBATES`)
      against current Polymarket docs — rates are "at Polymarket's sole discretion" and change.
- [ ] Re-verify the **taker fee** rate table and the `rate × p × (1−p)` formula are still current.
- [ ] Confirm the rebate model simplification (`rebate ≈ rebate_rate × fee`) is acceptable, or
      replace it with the pool-share formula before relying on rebate income.
- [ ] Calibrate the open constants — `sigma`, `k_gamma`, `gamma_ceiling`, `k_inv`, `tick_size`,
      `min_hours_to_resolution` — against real per-market data; the defaults are uncalibrated
      placeholders (see `docs/spec-phase2.md` Open Questions).
- [ ] Confirm the **tick size** matches each target market (Polymarket varies 0.01 / 0.001).

### Risk / safety
- [ ] Confirm the four stops fire as intended on live data: inventory cap, resolution stop,
      gamma stop, total-exposure cap.
- [ ] Set `max_inventory`, `max_total_exposure`, and `quote_size` to genuinely educational scale.
- [ ] Confirm **no inventory is ever held through resolution** (resolution stop + flatten).
- [ ] Decide and document the flatten mechanism near resolution (the runner cancels quotes;
      actively unwinding remaining inventory is **not yet implemented** — confirm acceptable
      or implement before live).
- [ ] Verify `LiveMakerExecutor.reconcile` correctly matches venue trades (`get_trades`) to
      resting orders for the **real** CLOB response shape (mocked only so far).

### Operational
- [ ] Wallet key in `.env` only (`POLYMARKET_WALLET_KEY`); never logged or committed.
- [ ] CLOB creds cached only to git-ignored `.clob_creds.json` (0600).
- [ ] Dry-run the live maker path against a **mocked** client end-to-end (place/cancel/reconcile).
- [ ] One opt-in `-m live` read-only check: discover a market, fetch its book, confirm the
      `end_date` parses and `T` is sane.
- [ ] Restrict `market_selector` to a small, deliberate set of markets for the first live run.

## Sign-off

> Live market making must not be enabled until the line below is completed by a human operator.

- Reviewed by: __________________________   Date: ____________
- Markets approved for first live run: ____________________________________________
- Capital caps confirmed (max_total_exposure / max_inventory / quote_size): ____________________
- Approved to arm live trading: **NO** (flip to YES only after all boxes above are checked)
