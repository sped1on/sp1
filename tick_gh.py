# -*- coding: utf-8 -*-
"""GitHub Actions version of the Polymarket demo bot. Same trading logic as
tick.py (byte-identical advance_state/resolve_pending core, already
verified against the batch backtest), but:
  - state.json / trades.jsonl live in a PRIVATE GitHub Gist (via API),
    not local files -- so they survive across ephemeral Actions runners
    without exposing trades in the public repo's git history.
  - strategy parameters are read from environment variables (populated
    from encrypted repo Secrets), so the exact tuned numbers aren't
    sitting in the public repo's source code.
  - `--loop-for SECONDS` runs the 5s tick loop for a bounded duration
    (one GitHub Actions job invocation), instead of forever.
"""
import json
import os
import time
import urllib.request

import broker
from notify import notify

GIST_TOKEN = os.environ.get("GIST_TOKEN")
GIST_ID = os.environ.get("GIST_ID")
STATE_FILENAME = "state.json"
TRADES_FILENAME = "trades.jsonl"

SYMBOLS = ["BTCUSDT", "ETHUSDT"]
SLUG_PREFIX = {"BTCUSDT": "btc", "ETHUSDT": "eth"}
CANDLE_MIN = 15
THRESH_PCT = float(os.environ.get("THRESH_PCT") or "0.10")
HOLD_MIN = int(os.environ.get("HOLD_MIN") or "10")
MAX_DEV_PCT = float(os.environ.get("MAX_DEV_PCT") or "0.0")
MIN_ENTRY_DIST_PCT = float(os.environ.get("MIN_ENTRY_DIST_PCT") or "0.15")
RISK_USD = float(os.environ.get("RISK_USD") or "50.0")
# Real-order sizing only (paper stats always use RISK_USD, untouched). Starts
# small; escalates to the bigger size after the first real order that both
# went through (wasn't rejected) AND won. Any loss still pauses the whole bot
# (paper or live) via the existing stop-loss pause, so this never compounds
# past one step without a human looking at it first.
LIVE_RISK_USD_START = float(os.environ.get("LIVE_RISK_USD_START") or "5.0")
LIVE_RISK_USD_ESCALATED = float(os.environ.get("LIVE_RISK_USD_ESCALATED") or "20.0")
MAX_ENTRY_PRICE = float(os.environ.get("MAX_ENTRY_PRICE") or "0.995")
PERIOD_MS = CANDLE_MIN * 60 * 1000
LIVE_TOLERANCE_MS = 120_000


def log(msg):
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} UTC  {msg}", flush=True)


LAST_HTTP_ERROR = {}  # url-prefix -> last error string this run, for gist diagnostics


def http_json(url, timeout=10, retries=2, method="GET", data=None, headers=None):
    last_err = None
    for attempt in range(retries + 1):
        try:
            body = json.dumps(data).encode() if data is not None else None
            req = urllib.request.Request(url, data=body, method=method,
                                          headers={"User-Agent": "polybot-gh/1.0", **(headers or {})})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except Exception as e:
            last_err = e
            if attempt < retries:
                time.sleep(1.5)
    log(f"HTTP error after retries: {method} {url} -> {last_err}")
    LAST_HTTP_ERROR[url.split("?")[0]] = f"{type(last_err).__name__}: {last_err}"
    return None


# ---------------------------- Gist-backed persistence ----------------------------

def gist_headers():
    return {"Authorization": f"token {GIST_TOKEN}", "Accept": "application/vnd.github+json"}


def gist_fetch():
    return http_json(f"https://api.github.com/gists/{GIST_ID}", headers=gist_headers())


def gist_update(files: dict):
    payload = {"files": {name: {"content": content} for name, content in files.items()}}
    result = http_json(f"https://api.github.com/gists/{GIST_ID}", method="PATCH", data=payload, headers=gist_headers())
    if result is None:
        log("  WARNING: failed to update gist (state may not persist this tick)")
    return result


def fetch_closed_klines(symbol, limit=30):
    data = http_json(f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol}&interval=1m&limit={limit}")
    if data is None:
        return []
    now_ms = int(time.time() * 1000)
    bars = []
    for k in data:
        open_time, o, h, l, c, close_time = k[0], float(k[1]), float(k[2]), float(k[3]), float(k[4]), k[6]
        if close_time < now_ms:
            is_live = (now_ms - close_time) < LIVE_TOLERANCE_MS
            bars.append(dict(open_time=open_time, open=o, high=h, low=l, close=c, is_live=is_live))
    return bars


def fetch_poly_price(symbol, period_start_ms, direction):
    window_ts = period_start_ms // 1000
    slug = f"{SLUG_PREFIX[symbol]}-updown-15m-{window_ts}"
    data = http_json(f"https://gamma-api.polymarket.com/events?slug={slug}")
    if not data:
        log(f"  Polymarket lookup MISS for slug={slug}")
        return None, slug, None
    try:
        m = data[0]["markets"][0]
        outcomes = json.loads(m["outcomes"])
        prices = json.loads(m["outcomePrices"])
        token_ids = json.loads(m["clobTokenIds"])
        idx = outcomes.index("Up" if direction == 1 else "Down")
        return float(prices[idx]), slug, token_ids[idx]
    except Exception as e:
        log(f"  Polymarket parse error for slug={slug}: {e}")
        return None, slug, None


def default_symbol_state():
    return dict(period_id=None, period_open=None, period_start_ts=None, minutes_elapsed=-1,
                side_dir=0, reached=False, dead=False, trigger_dir=0,
                sum_close=0.0, count=0, last_processed_ts=None, pending_trade=None)


def load_state(gist_data):
    content = gist_data["files"].get(STATE_FILENAME, {}).get("content", "").strip()
    if content:
        state = json.loads(content)
        state.setdefault("active_symbol", None)
        state.setdefault("paused", False)
        state.setdefault("symbols", {s: default_symbol_state() for s in SYMBOLS})
        for s in SYMBOLS:
            state["symbols"].setdefault(s, default_symbol_state())
        return state
    return dict(started_at=time.strftime("%Y-%m-%d %H:%M:%S"), active_symbol=None, paused=False,
                symbols={s: default_symbol_state() for s in SYMBOLS})


def load_trades_text(gist_data):
    return gist_data["files"].get(TRADES_FILENAME, {}).get("content", "")


def resolve_pending(sym, s, prev_sum, prev_count, state=None, trades_buffer=None):
    pt = s["pending_trade"]
    if pt is None:
        return
    if state is not None and state.get("active_symbol") == sym:
        state["active_symbol"] = None
    twap = prev_sum / prev_count if prev_count else pt["open"]
    diff = round(twap - pt["open"], 8)
    success = (diff >= 0) if pt["dir"] == 1 else (diff < 0)
    if pt["poly_price"] is not None:
        shares = RISK_USD / pt["poly_price"]
        pnl = shares * (1 - pt["poly_price"]) if success else -RISK_USD
    else:
        pnl = None
    trade = dict(
        symbol=sym, period_start_dt=time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(pt["period_start_ts"] / 1000)),
        dir="up" if pt["dir"] == 1 else "down", open=pt["open"], decision_price=pt["decision_price"],
        poly_price=pt["poly_price"], poly_slug=pt["poly_slug"], twap=round(twap, 6),
        success=bool(success), risk_usd=RISK_USD, pnl_usd=round(pnl, 4) if pnl is not None else None,
        entry_logged_at=pt["entry_logged_at"], resolved_at=time.strftime("%Y-%m-%d %H:%M:%S"),
    )
    if trades_buffer is not None:
        trades_buffer.append(trade)
    outcome = "WIN " if success else "LOSS"
    pnl_txt = f"${pnl:+.2f}" if pnl is not None else "n/a (no poly price)"
    log(f"  RESOLVED {sym} {trade['period_start_dt']} dir={trade['dir']} twap={twap:.4f} open={pt['open']:.4f} -> {outcome} pnl={pnl_txt}")
    live_txt = " · РЕАЛЬНЫЙ ОРДЕР" if pt.get("live_order_ok") else ""
    notify(f"{'✅ WIN' if success else '❌ LOSS'} {sym}",
           f"{trade['dir'].upper()} {pnl_txt}{live_txt}\nTWAP {twap:.2f} vs open {pt['open']:.2f}",
           tags="chart_with_upwards_trend" if success else "chart_with_downwards_trend")
    if not success and state is not None:
        state["paused"] = True
        log("  *** STOP-LOSS -- TRADING PAUSED. Set paused=false in the gist (or via --resume) to re-enable. ***")
        notify("⏸ Бот на паузе", f"Стоп-лосс по {sym} — торговля остановлена до paused=false", tags="warning")
    if success and pt.get("live_order_ok") and state is not None and state.get("live_stage") != "escalated":
        state["live_stage"] = "escalated"
        log(f"  *** first real order won -- live size escalated to ${LIVE_RISK_USD_ESCALATED:.0f} for future trades ***")


def advance_state(sym, s, new_bars, state=None, trades_buffer=None, poly_price_fn=fetch_poly_price, candidates=None):
    """candidates, if given, is a list SHARED across all symbols processed in
    the same tick: a would-be entry is appended to it rather than committed
    immediately, and the caller must call resolve_candidates(candidates,
    state) once after every symbol has been advanced this tick -- that's
    what actually decides (cheapest Polymarket price wins) and commits. If
    not given, this call owns a private list and resolves it itself before
    returning, so a single-symbol caller (e.g. tests) still gets an
    immediate commit exactly like before this feature existed."""
    owns_candidates = candidates is None
    if owns_candidates:
        candidates = []

    def resolve_fn(sym_, s_, prev_sum, prev_count):
        resolve_pending(sym_, s_, prev_sum, prev_count, state=state, trades_buffer=trades_buffer)

    for bar in new_bars:
        ts, o, h, l, c = bar["open_time"], bar["open"], bar["high"], bar["low"], bar["close"]
        period_id = ts // PERIOD_MS
        is_new_period = (s["period_id"] is None) or (period_id != s["period_id"])

        if is_new_period:
            prev_sum, prev_count = s["sum_close"], s["count"]
            if s["pending_trade"] is not None:
                resolve_fn(sym, s, prev_sum, prev_count)
            s.update(period_id=period_id, period_open=o, period_start_ts=ts, minutes_elapsed=0,
                      side_dir=0, reached=False, dead=False, trigger_dir=0,
                      sum_close=0.0, count=0, pending_trade=None)
        else:
            s["minutes_elapsed"] += 1

        s["sum_close"] += c
        s["count"] += 1

        me = s["minutes_elapsed"]
        period_open = s["period_open"]
        up_lvl = period_open * (1 + THRESH_PCT / 100)
        dn_lvl = period_open * (1 - THRESH_PCT / 100)

        if not s["dead"] and s["trigger_dir"] == 0 and 1 <= me <= HOLD_MIN:
            touched = l <= period_open <= h
            cur_side = 1 if l > period_open else (-1 if h < period_open else 0)
            if touched:
                s["dead"] = True
            elif s["side_dir"] == 0:
                s["side_dir"] = cur_side
            elif cur_side != s["side_dir"]:
                s["dead"] = True

            if not s["dead"] and s["side_dir"] != 0 and not s["reached"]:
                if (s["side_dir"] == 1 and h >= up_lvl) or (s["side_dir"] == -1 and l <= dn_lvl):
                    s["reached"] = True

            if not s["dead"] and MAX_DEV_PCT > 0 and s["side_dir"] != 0:
                too_far_up = s["side_dir"] == 1 and h >= period_open * (1 + MAX_DEV_PCT / 100)
                too_far_dn = s["side_dir"] == -1 and l <= period_open * (1 - MAX_DEV_PCT / 100)
                if too_far_up or too_far_dn:
                    s["dead"] = True

            if not s["dead"] and me == HOLD_MIN:
                cur_dist_pct = abs(c - period_open) / period_open * 100
                dist_ok = MIN_ENTRY_DIST_PCT <= 0 or cur_dist_pct >= MIN_ENTRY_DIST_PCT
                if s["side_dir"] != 0 and s["reached"] and dist_ok:
                    s["trigger_dir"] = s["side_dir"]
                    is_paused = state is not None and state.get("paused")
                    other_busy = state is not None and state.get("active_symbol") not in (None, sym)
                    dirtxt = "UP" if s["trigger_dir"] == 1 else "DOWN"
                    if is_paused:
                        log(f"  (skip, PAUSED after stop-loss) {sym} would-have-fired {dirtxt}")
                    elif other_busy:
                        log(f"  (skip, other asset active) {sym} would-have-fired {dirtxt} -- {state.get('active_symbol')} busy")
                    elif bar.get("is_live", True):
                        poly_price, slug, token_id = poly_price_fn(sym, s["period_start_ts"], s["trigger_dir"])
                        if poly_price is not None and poly_price > MAX_ENTRY_PRICE:
                            log(f"  (skip, price too high) {sym} {dirtxt} poly_price={poly_price:.3f} > cap {MAX_ENTRY_PRICE:.3f}")
                        else:
                            # Don't commit yet -- just register as a candidate. If BTC and ETH
                            # both fire in this same tick, resolve_candidates() (called once
                            # after ALL symbols have been processed) enters only the cheaper
                            # one. For the common single-candidate case this behaves exactly
                            # like an immediate commit (see resolve_candidates).
                            candidates.append(dict(
                                sym=sym, s=s, dirtxt=dirtxt, poly_price=poly_price, token_id=token_id,
                                slug=slug, decision_price=c, period_open=period_open,
                                period_start_ts=s["period_start_ts"], period_id=s["period_id"],
                                trigger_dir=s["trigger_dir"],
                            ))
                    else:
                        log(f"  (skip, backlog) {sym} would-have-fired {dirtxt} -- catching up, not live")

        s["last_processed_ts"] = ts

    if owns_candidates:
        resolve_candidates(candidates, state)
    return s


def resolve_candidates(candidates, state):
    """Commit exactly one candidate this tick: whichever has the cheapest
    Polymarket price (a lookup miss / None price is treated as worst, only
    chosen if it's the sole candidate). Runs real broker order + notify for
    the winner; everyone else is logged as skipped, never committed -- their
    trigger_dir was already set on their own state so they won't be
    re-evaluated again this period."""
    if not candidates:
        return
    chosen = min(candidates, key=lambda cand: cand["poly_price"] if cand["poly_price"] is not None else float("inf"))
    for cand in candidates:
        s, sym, dirtxt = cand["s"], cand["sym"], cand["dirtxt"]
        pp = f"{cand['poly_price']:.3f}" if cand["poly_price"] is not None else "N/A"
        if cand is not chosen:
            cpp = f"{chosen['poly_price']:.3f}" if chosen["poly_price"] is not None else "N/A"
            log(f"  (skip, pricier than alternative) {sym} {dirtxt} @ {pp} -- entered {chosen['sym']} @ {cpp} instead this tick")
            continue
        if s["period_id"] != cand["period_id"]:
            log(f"  (skip, stale candidate) {sym} {dirtxt} -- symbol state moved on before this could commit")
            continue
        s["pending_trade"] = dict(
            period_start_ts=cand["period_start_ts"], open=cand["period_open"], dir=cand["trigger_dir"],
            decision_price=cand["decision_price"], poly_price=cand["poly_price"], poly_slug=cand["slug"],
            entry_logged_at=time.strftime("%Y-%m-%d %H:%M:%S"),
        )
        if state is not None:
            state["active_symbol"] = sym
        log(f"  ENTRY {sym} {dirtxt} @ decision_price={cand['decision_price']:.4f} "
            f"open={cand['period_open']:.4f} poly_price={pp} slug={cand['slug']}")
        notify(f"📥 Вход {sym} {dirtxt}", f"Polymarket price {pp}\ndecision {cand['decision_price']:.2f} vs open {cand['period_open']:.2f}",
               tags="dart")
        escalated = state is not None and state.get("live_stage") == "escalated"
        live_size = LIVE_RISK_USD_ESCALATED if escalated else LIVE_RISK_USD_START
        result = broker.place_order(cand["token_id"], cand["trigger_dir"], cand["poly_price"], live_size)
        s["pending_trade"]["live_order_ok"] = result["ok"]
        if result["attempted"]:
            status = "OK" if result["ok"] else "REJECTED"
            log(f"  LIVE ORDER {sym} {dirtxt} ${live_size:.0f} -> {status}: {result['detail']}")
            notify(f"{'💰' if result['ok'] else '⚠️'} Реальный ордер {sym} {dirtxt} ${live_size:.0f}",
                   f"{status}: {result['detail'][:150]}",
                   tags="moneybag" if result["ok"] else "warning")


def process_symbol(sym, state, trades_buffer, candidates):
    s = state["symbols"][sym]
    bars = fetch_closed_klines(sym, limit=30)
    state.setdefault("_diag", {})[sym] = len(bars)
    if not bars:
        return
    new_bars = [b for b in bars if s["last_processed_ts"] is None or b["open_time"] > s["last_processed_ts"]]
    if not new_bars:
        return
    state["symbols"][sym] = advance_state(sym, s, new_bars, state=state, trades_buffer=trades_buffer, candidates=candidates)


def one_tick(gist_data):
    state = load_state(gist_data)
    trades_text = load_trades_text(gist_data)
    trades_buffer = []
    candidates = []
    LAST_HTTP_ERROR.clear()
    for sym in SYMBOLS:
        process_symbol(sym, state, trades_buffer, candidates)
    resolve_candidates(candidates, state)
    state["_diag_http_err"] = dict(LAST_HTTP_ERROR) or None
    state["_diag_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    if trades_buffer:
        new_lines = "\n".join(json.dumps(t, ensure_ascii=False) for t in trades_buffer)
        trades_text = (trades_text + ("\n" if trades_text and not trades_text.endswith("\n") else "") + new_lines + "\n")
        gist_update({STATE_FILENAME: json.dumps(state, indent=1), TRADES_FILENAME: trades_text})
    else:
        gist_update({STATE_FILENAME: json.dumps(state, indent=1)})
    return state


def main_loop_for(seconds):
    if not GIST_TOKEN or not GIST_ID:
        raise SystemExit("GIST_TOKEN / GIST_ID not set")
    log(f"starting bounded loop for {seconds}s")
    deadline = time.time() + seconds
    gist_data = gist_fetch()
    if gist_data is None:
        raise SystemExit("could not fetch gist -- aborting this run")
    while time.time() < deadline:
        try:
            one_tick(gist_data)
            gist_data = gist_fetch()  # refresh so next tick sees its own writes (and any external edits, e.g. --resume)
        except Exception as e:
            log(f"tick error (continuing): {e}")
        time.sleep(5)
    log("bounded loop finished for this job run")


if __name__ == "__main__":
    import sys
    args = sys.argv[1:]
    if "--loop-for" in args:
        secs = int(args[args.index("--loop-for") + 1])
        main_loop_for(secs)
    else:
        if not GIST_TOKEN or not GIST_ID:
            raise SystemExit("GIST_TOKEN / GIST_ID not set")
        gd = gist_fetch()
        if gd is not None:
            one_tick(gd)
