# -*- coding: utf-8 -*-
"""Check bot stats from anywhere -- reads the private Gist directly.
Usage: set GIST_TOKEN and GIST_ID env vars, then `python stats.py`."""
import json
import os
import urllib.request

GIST_TOKEN = os.environ["GIST_TOKEN"]
GIST_ID = os.environ["GIST_ID"]


def main():
    req = urllib.request.Request(f"https://api.github.com/gists/{GIST_ID}",
                                  headers={"Authorization": f"token {GIST_TOKEN}", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        gist = json.loads(r.read())

    trades_text = gist["files"].get("trades.jsonl", {}).get("content", "")
    trades = [json.loads(line) for line in trades_text.splitlines() if line.strip()]
    state_text = gist["files"].get("state.json", {}).get("content", "{}")
    state = json.loads(state_text)

    print(f"Пауза: {'ДА (остановлена после стопа)' if state.get('paused') else 'нет, торгует'}")
    print(f"Всего сделок: {len(trades)}")
    if not trades:
        return
    priced = [t for t in trades if t["poly_price"] is not None]
    for sym in sorted(set(t["symbol"] for t in trades)):
        sub = [t for t in trades if t["symbol"] == sym]
        wins = sum(1 for t in sub if t["success"])
        print(f"{sym}: {len(sub)} сделок, {wins} тейков, {len(sub)-wins} стопов, винрейт {100*wins/len(sub):.2f}%")
    if priced:
        total_pnl = sum(t["pnl_usd"] for t in priced)
        print(f"\nИтоговый P&L: ${total_pnl:+.2f}")
    print("\nПоследние 10:")
    for t in trades[-10:]:
        pp = f"{t['poly_price']*100:.1f}c" if t["poly_price"] is not None else "n/a"
        print(f"  {t['period_start_dt']}  {t['symbol']:8s} {t['dir']:4s}  poly={pp:>7s}  {'TP' if t['success'] else 'SL'}  pnl=${t['pnl_usd']:+.2f}")


if __name__ == "__main__":
    main()
