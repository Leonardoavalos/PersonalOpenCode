#!/usr/bin/env python3
"""Check the bridge without touching the real Telegram API.

Usage:
    OPENCODE_URL=http://127.0.0.1:4096 OPENCODE_PASSWORD=... python3 selftest.py

Covers two things separately: the event-stream state machine (synthetic events,
deterministic) and a real turn through the bridge (needs a live server).
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import bot  # noqa: E402

FAILED = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + (f"  [{detail}]" if detail else ""))
    if not ok:
        FAILED.append(label)


class RecorderTelegram:
    """Stands in for the Telegram client and records what would be sent."""

    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []
        self.edits: list[tuple[int, int, str]] = []
        self.keyboards: list[tuple[int, str, dict]] = []
        self.keyboard_edits: list[tuple[int, int, str, dict | None]] = []
        self.callbacks: list[tuple[str, str]] = []
        self.next_id = 100

    def send_message(self, chat_id: int, text: str) -> int:
        self.sent.append((chat_id, text))
        self.next_id += 1
        return self.next_id

    def edit_message(self, chat_id: int, message_id: int, text: str) -> None:
        self.edits.append((chat_id, message_id, text))

    def send_keyboard(self, chat_id: int, text: str, keyboard: dict) -> int:
        self.keyboards.append((chat_id, text, keyboard))
        self.next_id += 1
        return self.next_id

    def edit_keyboard(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        keyboard: dict | None = None,
    ) -> None:
        self.keyboard_edits.append((chat_id, message_id, text, keyboard))

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        self.callbacks.append((callback_id, text))


def test_event_stream() -> None:
    print("\n-- event stream state machine --")
    turns: dict[str, bot.Turn] = {}
    lock = threading.Lock()
    stream = bot.EventStream(oc=None, turns=turns, lock=lock)  # type: ignore[arg-type]

    turn = bot.Turn("ses_test", 1)
    turns["ses_test"] = turn

    def emit(kind: str, **data) -> None:
        payload = {"sessionID": "ses_test", **data}
        stream._apply({"type": kind, "data": payload})

    emit("session.reasoning.delta", delta="thinking", ordinal=0)
    check("reasoning deltas are not shown", turn.preview() == bot.STRINGS["thinking"], turn.preview())

    emit("session.text.delta", delta="Hola ", ordinal=0)
    emit("session.text.delta", delta="mundo", ordinal=0)
    check("text deltas accumulate", turn.preview() == "Hola mundo", turn.preview())

    emit("session.text.ended", text="Hola mundo.", ordinal=0)
    check("text.ended replaces the ordinal", turn.preview() == "Hola mundo.", turn.preview())

    emit("session.step.ended", cost=0.002, tokens={"output": 12})
    emit("session.step.ended", cost=0.003, tokens={"output": 8})
    check("step costs add up", abs(turn.cost - 0.005) < 1e-9, f"{turn.cost}")
    check("output tokens add up", turn.tokens_out == 20, str(turn.tokens_out))

    emit("session.execution.started")
    check("execution.started does not finish the turn", not turn.done.is_set())
    emit("session.execution.succeeded")
    check("execution.succeeded finishes the turn", turn.done.is_set(), turn.outcome)

    unrelated = bot.Turn("ses_other", 2)
    turns["ses_other"] = unrelated
    stream._apply({"type": "session.text.delta", "data": {"sessionID": "ses_unknown", "delta": "x"}})
    check("unknown sessions are ignored", unrelated.preview() == bot.STRINGS["thinking"])


def test_chunking() -> None:
    print("\n-- chunking --")
    chunks = bot.chunk_text("x" * 9500)
    check(
        "chunks stay under the limit and keep every byte",
        all(len(c) <= 4000 for c in chunks) and sum(len(c) for c in chunks) == 9500,
        f"{len(chunks)} parts",
    )
    check("empty text yields no chunks", bot.chunk_text("") == [])


def test_live_turn() -> None:
    print("\n-- live turn through the bridge --")
    bot.load_dotenv()
    base_url = bot.setting("OPENCODE_URL") or "http://127.0.0.1:4096"
    password = bot.setting("OPENCODE_PASSWORD")
    workspace = bot.setting("WORKSPACE") or str(ROOT)
    agent = bot.setting("OPENCODE_AGENT") or "remote"
    if not password:
        check("OPENCODE_PASSWORD is set", False)
        return

    oc = bot.OpenCode(base_url, password, workspace, agent)
    info = oc.ping()
    check("server reachable", bool(info.get("version")), str(info.get("version")))

    tg = RecorderTelegram()
    bridge = bot.Bridge(oc, tg, sessions_path=Path("/tmp/opencode/selftest-sessions.json"))
    bridge.sessions.clear()
    bridge.stream.start()

    started = time.time()
    bridge.run_turn(4242, "Escribí dos párrafos cortos sobre café. Texto plano.")
    elapsed = time.time() - started

    final = tg.edits[-1][2] if tg.edits else (tg.sent[0][1] if tg.sent else "")
    check("a reply was delivered", bool(final), f"{len(final)} chars")
    check("reply has a cost footer", "$" in final.splitlines()[-1], final.splitlines()[-1][:40] if final else "")
    check("turn finished under the timeout", elapsed < bot.TURN_TIMEOUT, f"{elapsed:.1f}s")
    print(f"      previews: {len(tg.edits)}  salidas extra: {len(tg.sent)}")
    print(f"      final: {final[:120]!r}...")


def test_permissions() -> None:
    print("\n-- permission broker --")
    replies: list[tuple[str, str, str]] = []

    class FakeOC:
        def permission_reply(self, session_id: str, request_id: str, decision: str) -> None:
            replies.append((session_id, request_id, decision))

    tg = RecorderTelegram()
    turns: dict[str, bot.Turn] = {}
    lock = threading.Lock()
    turn = bot.Turn("ses_perm", 99)
    turns["ses_perm"] = turn
    broker = bot.PermissionBroker(FakeOC(), tg, turns, lock, ttl=0.4)  # type: ignore[arg-type]

    request = {
        "id": "per_long_identifier_that_would_break_callback_data",
        "sessionID": "ses_perm",
        "action": "edit",
        "resources": ["notes.md", "spec.md"],
    }
    broker.ask(request)
    check("a prompt with a keyboard was sent", len(tg.keyboards) == 1, str(len(tg.keyboards)))
    token = next(iter(broker.pending))
    check("token is short enough for callback_data", len(f"{token}:reject") <= 64, token)

    label = broker.answer(token, "once")
    check("allow replies to the server", replies and replies[-1][2] == "once", str(replies[-1:]))
    check("answered request is removed", token not in broker.pending)
    check("no rejection counted on allow", turn.rejected == 0, str(turn.rejected))
    check("the button message is settled", bool(tg.keyboard_edits), str(tg.keyboard_edits[:1]))
    check("stale taps report settled", broker.answer(token, "once") == bot.STRINGS["perm_settled"])

    broker.ask({**request, "id": "per_second", "action": "shell", "resources": ["rm -rf build"]})
    token2 = next(iter(broker.pending))
    label2 = broker.answer(token2, "reject")
    check("reject replies to the server", replies[-1][2] == "reject", str(replies[-1:]))
    check("reject is counted on the turn", turn.rejected == 1, str(turn.rejected))
    check("reject label is reported", label2 == bot.STRINGS["perm_rejected"], label2)

    broker.ask({**request, "id": "per_third"})
    token3 = next(iter(broker.pending))
    time.sleep(0.9)
    check("an unanswered request expires", token3 not in broker.pending)
    check("expiry rejects on the server", replies[-1][2] == "reject", str(replies[-1:]))
    check("expiry increments the rejection count", turn.rejected == 2, str(turn.rejected))

    broker.ask({**request, "id": "per_fourth"})
    broker.cancel_session("ses_perm")
    check("aborting a session rejects its pending requests", not broker.pending)
    check("abort reason is visible", any("interrumpido" in e[2] for e in tg.keyboard_edits), str(tg.keyboard_edits[-1][2][-40:]))


def test_live_permission() -> None:
    print("\n-- live permission round trip --")
    bot.load_dotenv()
    base_url = bot.setting("OPENCODE_URL") or "http://127.0.0.1:4096"
    password = bot.setting("OPENCODE_PASSWORD")
    workspace = bot.setting("WORKSPACE") or str(ROOT)
    if not password:
        check("OPENCODE_PASSWORD is set", False)
        return

    oc = bot.OpenCode(base_url, password, workspace, "remote")
    sid = oc.create_session()
    prompt_id = oc.prompt(sid, "Create a file named bt_perm_probe.txt containing the word hola.")

    request = None
    for _ in range(60):
        time.sleep(0.5)
        pending = oc._request("GET", f"/api/session/{sid}/permission") or []
        if pending:
            request = pending[0]
            break
    check("a permission request arrived", request is not None, str((request or {}).get("action")))
    if request is None:
        return

    check("request names the file", request["resources"] == ["bt_perm_probe.txt"], str(request["resources"]))
    oc.permission_reply(sid, request["id"], "once")
    reply = oc.await_reply(sid, prompt_id)
    target = Path(workspace) / "bt_perm_probe.txt"
    check("approval let the write through", target.exists(), str(target))
    check("turn produced text", bool(reply), f"{len(reply)} chars")
    if target.exists():
        target.unlink()


def main() -> int:
    test_event_stream()
    test_chunking()
    test_permissions()
    test_live_turn()
    test_live_permission()
    print()
    if FAILED:
        print(f"{len(FAILED)} check(s) failed: {', '.join(FAILED)}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
