#!/usr/bin/env python3
"""Telegram <-> OpenCode bridge.

Step 1 of the personal remote-control build: a Telegram message becomes an
OpenCode prompt, and the assistant reply comes back as a chat message.

Standard library only. Requires a running `opencode serve` on the same machine.
"""

from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SESSIONS_PATH = ROOT / "sessions.json"

MAX_TELEGRAM_CHARS = 4000
POLL_SECONDS = 30
POLL_INTERVAL = 1.5
TURN_TIMEOUT = 900

HELP = """Personal OpenCode remote.

Send any text and it becomes a prompt in your session.

/start  this message
/new    start a fresh session
/status current session
/id     show your Telegram ids
"""


def load_dotenv() -> None:
    path = ROOT / ".env"
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def setting(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def log(message: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {message}", flush=True)


class OpenCodeError(RuntimeError):
    pass


class OpenCode:
    """Minimal HTTP client for the OpenCode server API."""

    def __init__(self, base_url: str, password: str, workspace: str, agent: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.password = password
        self.workspace = workspace
        self.agent = agent
        token = base64.b64encode(f"opencode:{password}".encode()).decode()
        self._auth = f"Basic {token}"

    def _request(
        self,
        method: str,
        path: str,
        payload: dict | None = None,
        timeout: float = 60,
    ) -> object:
        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(self.base_url + path, data=data, method=method)
        request.add_header("Authorization", self._auth)
        request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            raise OpenCodeError(f"{method} {path} -> HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise OpenCodeError(f"{method} {path} -> unreachable: {exc.reason}") from exc
        if not raw:
            return None
        body = json.loads(raw)
        # Routes wrap their payload in {"data": ...}, sometimes with a cursor.
        if isinstance(body, dict) and "data" in body:
            return body["data"]
        return body

    def ping(self) -> dict:
        return self._request("GET", "/api/info", timeout=10)

    def create_session(self) -> str:
        session = self._request(
            "POST",
            "/api/session",
            {
                "location": {"directory": self.workspace},
                "agent": self.agent,
                "title": "telegram",
            },
        )
        return session["id"]

    def prompt(self, session_id: str, text: str) -> str:
        message = self._request(
            "POST",
            f"/api/session/{session_id}/prompt",
            {"text": text},
            timeout=120,
        )
        return message["id"]

    def await_reply(self, session_id: str, prompt_id: str) -> str:
        """Poll until the turn's `idle` marker appears, then collect assistant text."""
        deadline = time.time() + TURN_TIMEOUT
        while time.time() < deadline:
            time.sleep(POLL_INTERVAL)
            messages = self._request("GET", f"/api/session/{session_id}/message?limit=50") or []
            # Messages arrive newest first. Anything before the prompt is this turn.
            index = next(
                (i for i, m in enumerate(messages) if m.get("id") == prompt_id),
                None,
            )
            if index is None:
                continue
            newer = messages[:index]
            if not any(m.get("type") == "idle" for m in newer):
                continue
            parts = []
            for message in reversed(newer):
                if message.get("type") != "assistant":
                    continue
                for item in message.get("content", []):
                    if item.get("type") == "text" and item.get("text"):
                        parts.append(item["text"])
            return "\n\n".join(parts).strip()
        raise OpenCodeError(f"turn did not finish within {TURN_TIMEOUT}s")


class Telegram:
    def __init__(self, token: str) -> None:
        self.base = f"https://api.telegram.org/bot{token}"

    def _call(self, method: str, payload: dict, timeout: float = 60) -> dict:
        data = json.dumps(payload).encode()
        request = urllib.request.Request(f"{self.base}/{method}", data=data, method="POST")
        request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            raise RuntimeError(f"telegram {method} -> HTTP {exc.code}: {detail}") from exc

    def get_updates(self, offset: int | None) -> list:
        payload: dict = {"timeout": POLL_SECONDS, "allowed_updates": ["message"]}
        if offset is not None:
            payload["offset"] = offset
        return self._call("getUpdates", payload, timeout=POLL_SECONDS + 15).get("result", [])

    def send(self, chat_id: int, text: str) -> None:
        for chunk in chunk_text(text):
            self._call(
                "sendMessage",
                {
                    "chat_id": chat_id,
                    "text": chunk,
                    "disable_web_page_preview": True,
                },
            )


def chunk_text(text: str, limit: int = MAX_TELEGRAM_CHARS) -> list[str]:
    """Split on line boundaries so no message exceeds Telegram's 4096 limit."""
    if not text:
        return ["(empty reply)"]
    chunks: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        while len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if len(current) + len(line) > limit:
            chunks.append(current)
            current = ""
        current += line
    if current:
        chunks.append(current)
    return chunks


def load_sessions() -> dict:
    if not SESSIONS_PATH.is_file():
        return {}
    try:
        return json.loads(SESSIONS_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def save_sessions(sessions: dict) -> None:
    tmp = SESSIONS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(sessions, indent=2), encoding="utf-8")
    tmp.replace(SESSIONS_PATH)


def main() -> int:
    load_dotenv()

    token = setting("TELEGRAM_BOT_TOKEN")
    password = setting("OPENCODE_PASSWORD")
    if not token or not password:
        log("missing TELEGRAM_BOT_TOKEN or OPENCODE_PASSWORD; see env.example")
        return 1

    allowed = {
        int(part)
        for part in setting("ALLOWED_USER_IDS").replace(" ", "").split(",")
        if part
    }
    workspace = setting("WORKSPACE") or str(ROOT)
    agent = setting("OPENCODE_AGENT") or "remote"
    base_url = setting("OPENCODE_URL") or "http://127.0.0.1:4096"

    oc = OpenCode(base_url, password, workspace, agent)
    tg = Telegram(token)

    try:
        info = oc.ping()
    except OpenCodeError as exc:
        log(f"cannot reach opencode at {base_url}: {exc}")
        return 1

    log(f"opencode {info.get('version')} at {base_url}, agent={agent}, workspace={workspace}")
    if not allowed:
        log("ALLOWED_USER_IDS is empty: every Telegram user is accepted")

    sessions = load_sessions()
    offset = None
    failures = 0

    while True:
        try:
            updates = tg.get_updates(offset)
            failures = 0
        except Exception as exc:  # noqa: BLE001 - keep serving through transient errors
            failures += 1
            log(f"poll failed ({failures}): {exc}")
            time.sleep(min(30, 2**failures))
            continue

        for update in updates:
            offset = update["update_id"] + 1
            message = update.get("message") or {}
            text = (message.get("text") or "").strip()
            chat_id = (message.get("chat") or {}).get("id")
            user_id = (message.get("from") or {}).get("id")
            if not text or chat_id is None or user_id is None:
                continue

            if allowed and user_id not in allowed:
                log(f"rejected user_id={user_id}")
                tg.send(chat_id, f"Not authorized.\nYour Telegram user id is {user_id}.")
                continue

            if text in ("/start", "/help"):
                tg.send(chat_id, HELP)
                continue

            if text == "/id":
                tg.send(chat_id, f"user id: {user_id}\nchat id: {chat_id}")
                continue

            if text == "/new":
                sessions.pop(str(chat_id), None)
                save_sessions(sessions)
                tg.send(chat_id, "New session started.")
                continue

            if text == "/status":
                record = sessions.get(str(chat_id), {})
                tg.send(
                    chat_id,
                    "session: {session}\nagent: {agent}\nworkspace: {workspace}".format(
                        session=record.get("session", "none yet"),
                        agent=agent,
                        workspace=workspace,
                    ),
                )
                continue

            record = sessions.setdefault(str(chat_id), {})
            try:
                session_id = record.get("session")
                if not session_id:
                    session_id = oc.create_session()
                    record["session"] = session_id
                    save_sessions(sessions)
                prompt_id = oc.prompt(session_id, text)
                reply = oc.await_reply(session_id, prompt_id)
            except OpenCodeError as exc:
                log(f"turn failed: {exc}")
                if "404" in str(exc) or "NotFound" in str(exc):
                    sessions.pop(str(chat_id), None)
                    save_sessions(sessions)
                    tg.send(chat_id, "Session was gone, reset it. Send the message again.")
                else:
                    tg.send(chat_id, f"OpenCode error: {exc}")
                continue

            log(f"turn ok: {len(reply)} chars")
            tg.send(chat_id, reply)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
