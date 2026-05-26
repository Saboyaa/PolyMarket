"""Summarize a scan_log.jsonl produced by the arbitrage runner.

Pure stdlib (no pandas) so it runs anywhere. Reports total scenarios, arbitrage
hit rates, a per-category breakdown, the closest-to-parity rows, and the time
span covered.

    uv run python scripts/analyze_log.py [path]   # default: scan_log.jsonl
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from decimal import Decimal
from pathlib import Path


def _load(path: Path) -> list[dict]:
    if not path.exists():
        print(f"No log file at {path} — run the bot first to generate one.")
        raise SystemExit(1)
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def _pct(n: int, total: int) -> str:
    return f"{(100.0 * n / total):.3f}%" if total else "0%"


def main() -> int:
    path = Path(sys.argv[1] if len(sys.argv) > 1 else "scan_log.jsonl")
    rows = _load(path)
    total = len(rows)
    if total == 0:
        print(f"{path} is empty.")
        return 0

    gross = sum(1 for r in rows if Decimal(r["gross_edge_per_share"]) > 0)
    actionable = sum(1 for r in rows if r.get("actionable"))
    executed = sum(1 for r in rows if r.get("executed"))

    print(f"=== {path} ===")
    print(f"Time span : {rows[0]['ts']}  ->  {rows[-1]['ts']}")
    print(f"\nTotal scenarios analyzed (logged near-misses + arbs): {total}")
    print(f"  gross arbs (ask_sum < $1)   : {gross:>6}  ({_pct(gross, total)})")
    print(f"  actionable (clears fees+min): {actionable:>6}  ({_pct(actionable, total)})")
    print(f"  executed (filled)           : {executed:>6}  ({_pct(executed, total)})")

    # --- Per-category breakdown --------------------------------------------
    by_cat: dict[str, dict[str, int]] = defaultdict(lambda: {"n": 0, "arb": 0, "act": 0})
    for r in rows:
        c = by_cat[r.get("category", "Other")]
        c["n"] += 1
        if Decimal(r["gross_edge_per_share"]) > 0:
            c["arb"] += 1
        if r.get("actionable"):
            c["act"] += 1

    print("\nBy category:")
    print(f"  {'category':16} {'rows':>6} {'gross arb':>10} {'actionable':>11}")
    for cat, c in sorted(by_cat.items(), key=lambda kv: -kv[1]["n"]):
        print(
            f"  {cat[:16]:16} {c['n']:>6} "
            f"{c['arb']:>4} ({_pct(c['arb'], c['n']):>7}) {c['act']:>4} ({_pct(c['act'], c['n']):>7})"
        )

    # --- Closest to an arbitrage -------------------------------------------
    closest = sorted(rows, key=lambda r: Decimal(r["ask_sum"]))[:10]
    print("\nClosest 10 to an arbitrage (lowest ask_sum first):")
    print(f"  {'ask_sum':>8} {'net_edge':>11}  category      condition_id")
    for r in closest:
        flag = "  <-- ARB" if Decimal(r["gross_edge_per_share"]) > 0 else ""
        print(
            f"  {r['ask_sum']:>8} {r['net_edge_per_share']:>11}  "
            f"{r.get('category', 'Other')[:12]:12}  {r['condition_id'][:14]}…{flag}"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
