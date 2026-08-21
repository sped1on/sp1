# -*- coding: utf-8 -*-
"""Cross-validate tick_gh.py's advance_state/resolve_pending (copy-adapted
from the already-verified tick.py, now writing to a trades_buffer list
instead of a file, plus a restructured resolve_fn closure) against the
same 100k-real-bar batch backtest used to verify the original."""
import os
import sys
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tick_gh as bot

SCRATCH = r"C:\Users\water\AppData\Local\Temp\claude\C--\c2a2fa92-5b08-4d17-b338-75f8300760e5\scratchpad"
sys.path.insert(0, SCRATCH)
os.chdir(SCRATCH)
from backtest_twap import run_twap  # noqa: E402

PASS = FAIL = 0


def check(name, got, want):
    global PASS, FAIL
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL':4s}  {name}" + ("" if ok else f"  got={got!r} want={want!r}"))
    PASS += ok
    FAIL += (not ok)


bot.log = lambda msg: None


def run_live_sim(df_bars, chunk_sizes):
    state = dict(active_symbol=None, paused=False, symbols={"TEST": bot.default_symbol_state()})
    trades_buffer = []
    s = state["symbols"]["TEST"]

    def fake_poly_price(sym, ts, direction):
        return None, "test-slug"

    bars = [dict(open_time=int(row.open_time), open=row.open, high=row.high, low=row.low, close=row.close, is_live=True)
            for row in df_bars.itertuples()]
    i = 0
    for cs in chunk_sizes:
        chunk = bars[i:i + cs]
        if not chunk:
            break
        s = bot.advance_state("TEST", s, chunk, state=state, trades_buffer=trades_buffer, poly_price_fn=fake_poly_price)
        i += cs
    if s["pending_trade"] is not None:
        bot.resolve_pending("TEST", s, s["sum_close"], s["count"], state=state, trades_buffer=trades_buffer)
    return trades_buffer


print("Cross-validate against run_twap() on ~100k real BTC bars (random chunk sizes 1-5)")
df_real = pd.read_csv(os.path.join(SCRATCH, "data", "BTCUSDT_1m_3y.csv"))
df_real["dt"] = pd.to_datetime(df_real["open_time"], unit="ms", utc=True)
df_real = df_real.drop_duplicates("open_time").sort_values("dt").reset_index(drop=True)
subset = df_real.iloc[300000:400000].reset_index(drop=True)

batch = run_twap(subset, candle_min=15, thresh_pct=0.10, hold_min=10, max_dev_pct=0.0, min_entry_dist_pct=0.15)
batch_ts_str = pd.to_datetime(batch["period_start_dt"]).dt.strftime("%Y-%m-%d %H:%M:%S")
batch_dir_str = batch["dir"].map({1: "up", -1: "down"})
batch_set = set(zip(batch_ts_str, batch_dir_str, batch["success"]))

import random
random.seed(7)
sizes = []
remaining = len(subset)
while remaining > 0:
    c = random.choice([1, 1, 1, 2, 3, 5])
    c = min(c, remaining)
    sizes.append(c)
    remaining -= c

live = run_live_sim(subset, sizes)
live_set = set((pd.Timestamp(t["period_start_dt"]).strftime("%Y-%m-%d %H:%M:%S"), t["dir"], t["success"]) for t in live)

check(f"trade COUNT matches (batch={len(batch_set)} vs live={len(live_set)})", len(batch_set), len(live_set))
check("identical (period_start, dir, success) sets", batch_set == live_set, True)
if batch_set != live_set:
    print("  only in batch:", sorted(batch_set - live_set)[:5])
    print("  only in live: ", sorted(live_set - batch_set)[:5])

print(f"\n{'='*70}\n  {PASS} passed, {FAIL} failed\n{'='*70}")
raise SystemExit(1 if FAIL else 0)
