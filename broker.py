# -*- coding: utf-8 -*-
"""Real order placement on Polymarket's CLOB. Completely inert by default --
every function below is a safe no-op unless LIVE_TRADING=true is set in the
environment. Nothing about tick.py's paper-trading stats (trades.jsonl,
win rate) depends on this module; it's a parallel, additive real-money path.

Credentials (POLY_PRIVATE_KEY, optionally POLY_FUNDER) are read ONLY from
the environment. This file never writes them anywhere, never logs them, and
they should never be pasted into a chat with an assistant -- generate them
yourself with derive_creds.py, run directly by you in your own terminal.
"""
import os

LIVE_TRADING = os.environ.get("LIVE_TRADING", "false").strip().lower() == "true"
CLOB_HOST = "https://clob.polymarket.com"
CHAIN_ID = 137

_client = None


def _get_client():
    global _client
    if _client is not None:
        return _client
    from py_clob_client.client import ClobClient
    key = os.environ["POLY_PRIVATE_KEY"]
    funder = os.environ.get("POLY_FUNDER") or None
    c = ClobClient(CLOB_HOST, key=key, chain_id=CHAIN_ID, signature_type=0, funder=funder)
    c.set_api_creds(c.create_or_derive_api_creds())
    _client = c
    return c


def place_order(token_id, direction, price, risk_usd):
    """Buys `risk_usd` worth of the outcome token identified by token_id
    (caller already picked the Up or Down token id based on `direction`).
    Never raises: any failure (auth, network, insufficient balance, bad
    params) is caught and returned so the tick loop can log it and move on.

    Returns: {"attempted": bool, "ok": bool, "detail": str}
    "attempted": False means we didn't even try (LIVE_TRADING off, or
    missing token_id/price) -- this is the normal case until you flip the
    switch.
    """
    if not LIVE_TRADING:
        return {"attempted": False, "ok": False, "detail": "LIVE_TRADING not enabled"}
    if not token_id or price is None:
        return {"attempted": False, "ok": False, "detail": "missing token_id/price"}
    try:
        from py_clob_client.clob_types import OrderArgs, OrderType
        from py_clob_client.order_builder.constants import BUY

        client = _get_client()
        size = round(risk_usd / price, 2)
        if size <= 0:
            return {"attempted": False, "ok": False, "detail": f"computed size {size} <= 0"}
        order = OrderArgs(token_id=token_id, price=round(price, 3), size=size, side=BUY)
        signed = client.create_order(order)
        resp = client.post_order(signed, OrderType.GTC)
        ok = bool(resp) and not (isinstance(resp, dict) and resp.get("error"))
        return {"attempted": True, "ok": ok, "detail": str(resp)[:500]}
    except Exception as e:
        return {"attempted": True, "ok": False, "detail": f"{type(e).__name__}: {e}"}
