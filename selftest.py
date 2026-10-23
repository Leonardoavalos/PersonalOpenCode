#!/usr/bin/env python3
"""Check the OpenCode half of the bridge without touching Telegram.

Usage:
    python3 selftest.py            # reads .env
    OPENCODE_URL=... OPENCODE_PASSWORD=... python3 selftest.py

Prints PASS/FAIL per check and exits non-zero on failure.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import bot  # noqa: E402


def check(label: str, ok: bool, detail: str = "") -> bool:
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + (f"  [{detail}]" if detail else ""))
    return ok


def main() -> int:
    bot.load_dotenv()
    base_url = bot.setting("OPENCODE_URL") or "http://127.0.0.1:4096"
    password = bot.setting("OPENCODE_PASSWORD")
    workspace = bot.setting("WORKSPACE") or str(ROOT)
    agent = bot.setting("OPENCODE_AGENT") or "remote"

    if not password:
        print("OPENCODE_PASSWORD is not set; see env.example")
        return 1

    ok = True
    oc = bot.OpenCode(base_url, password, workspace, agent)

    info = oc.ping()
    ok &= check("server reachable", bool(info.get("version")), str(info.get("version")))

    sid = oc.create_session()
    ok &= check("session created", sid.startswith("ses_"), sid)

    prompt_id = oc.prompt(sid, "Reply with exactly: pong")
    reply = oc.await_reply(sid, prompt_id)
    ok &= check("turn completed", bool(reply), f"{len(reply)} chars")

    messages = oc._request("GET", f"/api/session/{sid}/message?limit=20") or []
    used = next((m.get("agent") for m in messages if m.get("type") == "assistant"), None)
    ok &= check(f"agent '{agent}' was used", used == agent, str(used))

    chunks = bot.chunk_text("x" * 9500)
    ok &= check(
        "chunking stays under 4000 chars",
        all(len(c) <= 4000 for c in chunks) and sum(len(c) for c in chunks) == 9500,
        f"{len(chunks)} parts",
    )

    print("\nall checks passed" if ok else "\nsome checks failed")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
