# -*- coding: utf-8 -*-
"""Push notifications to a phone via ntfy.sh -- no account, no token, just a
topic name (set NTFY_TOPIC). Install the ntfy app (Android/iOS) and subscribe
to the same topic name to receive them. Pick a long, hard-to-guess topic --
anyone who knows it can read (and post to) it, since ntfy.sh is a public
relay with no auth on the free tier.

Fully inert if NTFY_TOPIC isn't set -- safe to import/call unconditionally.
"""
import os
import urllib.request

NTFY_TOPIC = os.environ.get("NTFY_TOPIC")


def notify(title, message, tags=""):
    if not NTFY_TOPIC:
        return
    try:
        req = urllib.request.Request(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=message.encode("utf-8"),
            method="POST",
            headers={
                "Title": title,
                "Tags": tags,
                "User-Agent": "polybot-notify/1.0",
            },
        )
        urllib.request.urlopen(req, timeout=8)
    except Exception:
        pass  # never let a notification failure break the trading loop
