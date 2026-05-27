"""Live terminal dashboard for the paper market maker — "is it working?".

Runs the real MMRunner in paper mode over a sample of the viable universe
(longer-dated, mid/low-price Politics & Sports — see scripts/market_study.py)
against LIVE books, and redraws a table each refresh so you can watch it work:
per-market resting quotes, signed inventory, PnL, status, and the countdown to
each market's next requote (per-market cadence). No wallet, no real orders.

    uv run python scripts/dashboard_mm.py [N] [REFRESH_SECONDS]   # default 10, 2

Ctrl-C to stop (prints a final summary).
"""

from __future__ import annotations

import json
import sys
import time
from datetime import UTC, datetime
from decimal import Decimal

import httpx

from polymarket_bot.common.config import Config, MarketMakingConfig
from polymarket_bot.common.models import Market
from polymarket_bot.phase2_market_making.cli import build_runner

GAMMA = "https://gamma-api.polymarket.com"
_MID_LO, _MID_HI, _MIN_DAYS = 0.05, 0.60, 7.0  # the viable-universe filter

# ANSI helpers (degrade gracefully if piped to a file).
_CLEAR, _HOME = "\033[2J", "\033[H"
_DIM, _BOLD, _RESET = "\033[2m", "\033[1m", "\033[0m"
_GREEN, _RED, _YELLOW, _CYAN = "\033[32m", "\033[31m", "\033[33m", "\033[36m"

_MM = MarketMakingConfig(
    sigma="6.7", base_spread="0.005", k_gamma="0.0008", gamma_ceiling="51",
    tick_size="0.001", quote_size="10", min_hours_to_resolution="6",
    estimate_sigma=True, min_cadence_seconds="60", max_cadence_seconds="172800",
)


def _sample(n: int) -> list[Market]:
    r = httpx.get(f"{GAMMA}/markets",
                  params={"active": "true", "closed": "false", "order": "volume24hr",
                          "ascending": "false", "limit": 250}, timeout=20.0)
    r.raise_for_status()
    out: list[Market] = []
    for m in r.json():
        ids = m.get("clobTokenIds")
        if isinstance(ids, str):
            ids = json.loads(ids)
        if not ids or len(ids) != 2:
            continue
        end = m.get("endDate")
        if not end:
            continue
        days = (datetime.fromisoformat(end.replace("Z", "+00:00")) - datetime.now(UTC)).days
        try:
            p = float(m.get("lastTradePrice"))
        except (TypeError, ValueError):
            continue
        if days < _MIN_DAYS or not (_MID_LO < p < _MID_HI):
            continue
        out.append(Market(
            condition_id=str(m["conditionId"]), question=str(m.get("question", "")),
            category="Other", yes_token_id=str(ids[0]), no_token_id=str(ids[1]),
            end_date=datetime.fromisoformat(end.replace("Z", "+00:00")),
        ))
        if len(out) >= n:
            break
    return out


def _fmt_pnl(x: Decimal) -> str:
    c = _GREEN if x > 0 else _RED if x < 0 else _DIM
    return f"{c}{float(x):+.4f}{_RESET}"


def _render(runner, markets, scan: int, started: float, error: str = "") -> None:
    now = runner._clock()
    s = runner.stats
    quoting = sum(1 for ex in runner._executors.values() if ex.open_orders)
    resting = sum(len(ex.open_orders) for ex in runner._executors.values())
    dot = _GREEN + "●" + _RESET if quoting else _YELLOW + "○" + _RESET
    elapsed = int(time.time() - started)

    lines = [_CLEAR + _HOME]
    lines.append(f"{_BOLD}{dot} PolyMarket paper market maker{_RESET}  "
                 f"{_DIM}scan {scan} · {elapsed}s elapsed · PAPER (no wallet){_RESET}")
    lines.append(f"  markets={len(runner._executors)}  quoting={_GREEN}{quoting}{_RESET}  "
                 f"resting_orders={resting}  fills={_CYAN}{s['fills']}{_RESET}  "
                 f"stopped={s['stopped']}  skipped={s['skipped']}")
    lines.append(f"  exposure={s['total_exposure']} USDC   net PnL={_fmt_pnl(s['net_pnl'])} USDC")
    lines.append("")
    lines.append(f"  {_BOLD}{'mid':>6} {'bid':>6} {'ask':>6} {'inv':>5} "
                 f"{'pnl':>9} {'status':>8} {'requote':>8}  market{_RESET}")

    for m in markets:
        ex = runner._executors.get(m.condition_id)
        if ex is None:
            continue
        orders = ex.open_orders
        hist = runner._mid_history.get(m.condition_id)
        mid = f"{hist[-1]:.3f}" if hist else "-"
        bid = next((f"{o.price}" for o in orders if o.buy), "-")
        ask = next((f"{o.price}" for o in orders if not o.buy), "-")
        inv = ex.inventory.net_yes
        due = runner._next_due.get(m.condition_id)
        secs = int((due - now).total_seconds()) if due else 0
        requote = f"{max(secs, 0)}s" if secs < 90 else f"{secs // 60}m"
        if orders:
            status = f"{_GREEN}QUOTING{_RESET}"
        elif s["total_exposure"] >= runner._config.risk.max_total_exposure:
            status = f"{_YELLOW}CAPPED{_RESET}"
        else:
            status = f"{_DIM}STOPPED{_RESET}"
        lines.append(f"  {mid:>6} {bid:>6} {ask:>6} {float(inv):>5.0f} "
                     f"{_fmt_pnl(ex.inventory.net_pnl):>18} {status:>17} {requote:>8}  "
                     f"{m.question[:40]}")
    lines.append("")
    if error:
        lines.append(f"{_YELLOW}⚠ last scan error (retrying): {error[:70]}{_RESET}")
    lines.append(f"{_DIM}Ctrl-C to stop. Fills need the live book to cross a resting "
                 f"quote — rare on short timescales.{_RESET}")
    sys.stdout.write("\n".join(lines) + "\n")
    sys.stdout.flush()


def main() -> int:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    refresh = float(sys.argv[2]) if len(sys.argv) > 2 else 2.0
    print("Sampling the viable universe (Politics/Sports, 7d+, p 0.05-0.6)...")
    try:
        markets = _sample(n)
    except httpx.HTTPError as exc:
        print(f"Network error reaching Polymarket ({type(exc).__name__}): {exc}\n"
              "Check your connection and retry.")
        return 1
    if not markets:
        print("No markets matched the filter.")
        return 1

    class _Bounded:
        def discover_markets(self, _sel):
            return markets

    runner = build_runner(Config(mm=_MM), market_source=_Bounded(), quote_log=None)
    started = time.time()
    scan = 0
    last_error = ""
    try:
        while True:
            scan += 1
            try:
                runner.scan_once()
                last_error = ""
            except Exception as exc:  # noqa: BLE001 - a transient blip must not kill the monitor
                last_error = f"{type(exc).__name__}: {exc}"
            _render(runner, markets, scan, started, last_error)
            time.sleep(refresh)
    except KeyboardInterrupt:
        s = runner.stats
        print(f"\n{_RESET}Stopped after {scan} scans. "
              f"fills={s['fills']} exposure={s['total_exposure']} net PnL={s['net_pnl']} USDC")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
