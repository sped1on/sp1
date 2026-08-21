# -*- coding: utf-8 -*-
"""One-time helper: creates the private Gist used for bot state storage.
Run once: python create_gist.py
It will ask for your token (typed, not stored anywhere) and print the GIST_ID."""
import getpass
import json
import urllib.request

token = getpass.getpass("Вставьте ваш GitHub token (gist scope) и нажмите Enter: ")

payload = {
    "description": "polybot state",
    "public": False,
    "files": {
        "state.json": {"content": "{}"},
        "trades.jsonl": {"content": ""},
    },
}

req = urllib.request.Request(
    "https://api.github.com/gists",
    data=json.dumps(payload).encode("utf-8"),
    method="POST",
    headers={
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
        "Content-Type": "application/json",
        "User-Agent": "polybot-setup/1.0",
    },
)

try:
    with urllib.request.urlopen(req, timeout=15) as r:
        result = json.loads(r.read())
    print()
    print("Готово! GIST_ID =", result["id"])
    print("Сохраните это значение — понадобится для Secrets в шаге 5.")
except urllib.error.HTTPError as e:
    print("Ошибка от GitHub:", e.code, e.read().decode())

input("\nНажмите Enter, чтобы закрыть окно...")
