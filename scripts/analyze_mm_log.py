"""Summarize an mm_log.jsonl produced by the market-making runner.

Pure stdlib (no pandas). Reports the time span, quote/stop counts, average
quoted half-spread, and a per-market breakdown of final inventory and PnL
(net PnL, fees paid, rebates earned).

    uv run python scripts/analyze_mm_log.py [path]   # default: mm_log.jsonl
"""

from __future__ import annotations

import json
import sys
from decimal import Decimal
from pathlib import Path


def _load(path: Path) -> list[dict]:
    if not path.exists():
        print(f"No log file at {path} — run the MM bot first to generate one.")
        raise SystemExit(1)
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def _pct(n: int, total: int) -> str:
    return f"{(100.0 * n / total):.1f}%" if total else "0%"


def _dec(value: object) -> Decimal:
    return Decimal(str(value)) if value is not None else Decimal(0)


def main() -> int:
    path = Path(sys.argv[1] if len(sys.argv) > 1 else "mm_log.jsonl")
    rows = _load(path)
    total = len(rows)
    if total == 0:
        print(f"{path} is empty.")
        return 0

    quoted = sum(1 for r in rows if r.get("quoted"))
    stopped = total - quoted
    half_spreads = [_dec(r["half_spread"]) for r in rows if r.get("half_spread") is not None]
    avg_half = sum(half_spreads, Decimal(0)) / len(half_spreads) if half_spreads else Decimal(0)

    print(f"=== {path} ===")
    print(f"Time span : {rows[0]['ts']}  ->  {rows[-1]['ts']}")
    print(f"\nSnapshots logged           : {total}")
    print(f"  quoted (posted a quote)  : {quoted:>6}  ({_pct(quoted, total)})")
    print(f"  stopped (pulled quotes)  : {stopped:>6}  ({_pct(stopped, total)})")
    print(f"  avg quoted half-spread   : {avg_half:.5f}")

    # --- Per-market final state (last row wins per condition id) -----------
    last: dict[str, dict] = {}
    for r in rows:
        last[r["condition_id"]] = r

    print("\nPer market (final snapshot):")
    print(
        f"  {'condition_id':16} {'category':10} {'net_yes':>9} "
        f"{'net_pnl':>11} {'fees':>9} {'rebates':>9}"
    )
    agg_pnl = Decimal(0)
    for cid, r in sorted(last.items(), key=lambda kv: _dec(kv[1]["net_pnl"]), reverse=True):
        pnl = _dec(r["net_pnl"])
        agg_pnl += pnl
        print(
            f"  {cid[:16]:16} {str(r.get('category', ''))[:10]:10} "
            f"{_dec(r['net_yes']):>9} {pnl:>11.5f} "
            f"{_dec(r['fees_paid']):>9.5f} {_dec(r['rebates_earned']):>9.5f}"
        )
    print(f"\nAggregate net PnL across {len(last)} market(s): {agg_pnl:.5f} USDC")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
