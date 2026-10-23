#!/usr/bin/env python3
"""Telegram <-> OpenCode bridge.

A Telegram message becomes an OpenCode prompt, and the assistant reply streams
back into the chat as it is generated.

Standard library only. Requires a running `opencode serve` on the same machine.

Design notes
------------
The HTTP API does not expose partial assistant text through /message: a message
stays empty until the turn completes. Live output is only available from the
/api/event SSE stream, so one background thread consumes that stream and routes
events by session id. The final answer is always re-read from /message, so a
dropped stream degrades the preview but never the reply.
"""

from __future__ import annotations

import base64
import json
import os
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SESSIONS_PATH = ROOT / "sessions.json"

MAX_TELEGRAM_CHARS = 4000
POLL_SECONDS = 30
TURN_TIMEOUT = 900
PREVIEW_INTERVAL = 1.2
PREVIEW_MIN_CHARS = 24
PERMISSION_TTL = 120

# User-facing copy is Spanish because both users of this bot are Spanish
# speakers. Keep it neutral and change it here if that ever stops being true.
STRINGS = {
    "help": (
        "OpenCode personal.\n\n"
        "Mandá texto y va como prompt a tu sesión.\n\n"
        "/start   esta ayuda\n"
        "/new     sesión nueva\n"
        "/status  estado de la sesión\n"
        "/abort   cortar el turno en curso\n"
        "/id      tus ids de Telegram"
    ),
    "thinking": "procesando...",
    "tool": "usando {tool}...",
    "unauthorized": "No autorizado.\nTu user id de Telegram es {user_id}.",
    "new_session": "Sesión nueva.",
    "busy": "Hay un turno en curso. Usá /abort para cortarlo.",
    "aborted": "Turno interrumpido.",
    "nothing_running": "No hay ningún turno en curso.",
    "gone": "La sesión ya no existía; la reinicié. Mandá el mensaje de nuevo.",
    "empty": "(respuesta vacía)",
    "perm_edit": "El agente quiere editar: {what}",
    "perm_shell": "El agente quiere ejecutar:\n{what}",
    "perm_other": "El agente quiere {action}: {what}",
    "perm_allow": "Permitir",
    "perm_deny": "Rechazar",
    "perm_allowed": "Permitido",
    "perm_rejected": "Rechazado",
    "perm_expired": "expiró",
    "perm_settled": "ya estaba resuelto",
    "perm_not_yours": "No autorizado.",
    "perm_blocked": "No pude seguir: rechazaste el permiso.",
}


def load_dotenv() -> None:
    path = ROOT / ".env"
    if not path.is_file():
        return
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        # Last assignment in the file wins, like every other dotenv loader.
        values[key.strip()] = value.strip().strip("\"'")
    for key, value in values.items():
        # A variable already exported in the environment still takes precedence.
        os.environ.setdefault(key, value)


def setting(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def log(message: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {message}", flush=True)


class OpenCodeError(RuntimeError):
    pass


def chunk_text(text: str, limit: int = MAX_TELEGRAM_CHARS) -> list[str]:
    """Split on line boundaries so no message exceeds Telegram's 4096 limit."""
    if not text:
        return []
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


class Turn:
    """Live state of one assistant turn."""

    def __init__(self, session_id: str, chat_id: int) -> None:
        self.session_id = session_id
        self.chat_id = chat_id
        self.parts: dict[int, str] = {}
        self.current_ordinal = 0
        self.tool: str | None = None
        self.cost = 0.0
        self.tokens_out = 0
        self.done = threading.Event()
        self.outcome = ""
        self.error = ""
        self.started = time.time()
        self.aborted = False
        self.rejected = 0
        self.previewed = ""

    def add_delta(self, ordinal: int, delta: str) -> None:
        self.current_ordinal = ordinal
        self.parts[ordinal] = self.parts.get(ordinal, "") + delta

    def set_part(self, ordinal: int, text: str) -> None:
        self.parts[ordinal] = text

    def preview(self) -> str:
        body = "\n\n".join(self.parts[key] for key in sorted(self.parts) if self.parts[key])
        if not body:
            return STRINGS["thinking"]
        if self.tool:
            return f"{body}\n\n· {STRINGS['tool'].format(tool=self.tool)}"
        return body


class EventStream:
    """Consumes /api/event and applies events to the matching live Turn."""

    def __init__(self, oc: "OpenCode", turns: dict[str, Turn], lock: threading.Lock) -> None:
        self.oc = oc
        self.turns = turns
        self.lock = lock
        self.on_permission = None
        self.stopped = threading.Event()
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        self.thread = threading.Thread(target=self._run, name="event-stream", daemon=True)
        self.thread.start()

    def _run(self) -> None:
        backoff = 1.0
        while not self.stopped.is_set():
            try:
                self._consume()
                backoff = 1.0
            except Exception as exc:  # noqa: BLE001 - reconnect on any stream failure
                log(f"event stream dropped: {exc}")
            if self.stopped.is_set():
                return
            time.sleep(backoff)
            backoff = min(10.0, backoff * 2)

    def _consume(self) -> None:
        request = urllib.request.Request(self.oc.base_url + "/api/event")
        request.add_header("Authorization", self.oc.auth_header)
        request.add_header("Accept", "text/event-stream")
        with urllib.request.urlopen(request, timeout=90) as response:
            for raw in response:
                if self.stopped.is_set():
                    return
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                try:
                    event = json.loads(line[5:].strip())
                except json.JSONDecodeError:
                    continue
                self._apply(event)

    def _apply(self, event: dict) -> None:
        kind = event.get("type", "")
        payload = event.get("data") or {}
        session_id = payload.get("sessionID")
        if not session_id:
            return
        with self.lock:
            turn = self.turns.get(session_id)
        if turn is None:
            return

        if kind == "session.text.delta":
            turn.add_delta(payload.get("ordinal", 0), payload.get("delta", ""))
        elif kind == "session.text.ended":
            turn.set_part(payload.get("ordinal", 0), payload.get("text", ""))
        elif kind == "session.tool.called":
            turn.tool = payload.get("tool") or payload.get("name") or "tool"
        elif kind == "session.tool.success":
            turn.tool = None
        elif kind == "session.step.ended":
            turn.cost += float(payload.get("cost") or 0.0)
            turn.tokens_out += int((payload.get("tokens") or {}).get("output") or 0)
        elif kind.startswith("session.execution.") and kind != "session.execution.started":
            turn.outcome = kind.rsplit(".", 1)[-1]
            turn.done.set()
        elif kind == "permission.asked":
            handler = self.on_permission
            if handler is not None:
                handler(payload)


class OpenCode:
    """Minimal HTTP client for the OpenCode server API."""

    def __init__(self, base_url: str, password: str, workspace: str, agent: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.workspace = workspace
        self.agent = agent
        token = base64.b64encode(f"opencode:{password}".encode()).decode()
        self.auth_header = f"Basic {token}"

    def _request(
        self,
        method: str,
        path: str,
        payload: dict | None = None,
        timeout: float = 60,
    ) -> object:
        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(self.base_url + path, data=data, method=method)
        request.add_header("Authorization", self.auth_header)
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

    def interrupt(self, session_id: str) -> None:
        self._request("POST", f"/api/session/{session_id}/interrupt", {}, timeout=30)

    def permission_reply(self, session_id: str, request_id: str, decision: str) -> None:
        self._request(
            "POST",
            f"/api/session/{session_id}/permission/{request_id}/reply",
            {"decision": decision},
            timeout=30,
        )

    def messages(self, session_id: str, limit: int = 50) -> list:
        return self._request("GET", f"/api/session/{session_id}/message?limit={limit}") or []

    def final_reply(self, session_id: str, prompt_id: str) -> str:
        """Authoritative assistant text for the turn that started with prompt_id."""
        messages = self.messages(session_id)
        index = next((i for i, m in enumerate(messages) if m.get("id") == prompt_id), None)
        if index is None:
            return ""
        parts: list[str] = []
        for message in reversed(messages[:index]):
            if message.get("type") != "assistant":
                continue
            for item in message.get("content", []):
                if item.get("type") == "text" and item.get("text"):
                    parts.append(item["text"])
        return "\n\n".join(parts).strip()

    def await_reply(self, session_id: str, prompt_id: str) -> str:
        """Polling fallback used when no event stream is available."""
        deadline = time.time() + TURN_TIMEOUT
        while time.time() < deadline:
            time.sleep(1.2)
            messages = self.messages(session_id)
            index = next((i for i, m in enumerate(messages) if m.get("id") == prompt_id), None)
            if index is None:
                continue
            if not any(m.get("type") == "idle" for m in messages[:index]):
                continue
            return self.final_reply(session_id, prompt_id)
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
            body = exc.read().decode("utf-8", "replace")
            if exc.code == 429:
                try:
                    wait = json.loads(body)["parameters"]["retry_after"]
                except Exception:  # noqa: BLE001
                    wait = 3
                log(f"flood control, waiting {wait}s")
                time.sleep(wait)
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    return json.loads(response.read())
            raise RuntimeError(f"telegram {method} -> HTTP {exc.code}: {body[:300]}") from exc

    def get_updates(self, offset: int | None) -> list:
        payload: dict = {
            "timeout": POLL_SECONDS,
            "allowed_updates": ["message", "callback_query"],
        }
        if offset is not None:
            payload["offset"] = offset
        return self._call("getUpdates", payload, timeout=POLL_SECONDS + 15).get("result", [])

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        try:
            self._call(
                "answerCallbackQuery",
                {"callback_query_id": callback_id, "text": text[:200]},
            )
        except Exception as exc:  # noqa: BLE001 - a stale callback is not an error
            log(f"answerCallbackQuery failed: {exc}")

    def send_keyboard(self, chat_id: int, text: str, keyboard: dict) -> int:
        result = self._call(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": text[:MAX_TELEGRAM_CHARS],
                "reply_markup": keyboard,
                "disable_web_page_preview": True,
            },
        )
        return result.get("result", {}).get("message_id")

    def edit_keyboard(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        keyboard: dict | None = None,
    ) -> None:
        payload: dict = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text[:MAX_TELEGRAM_CHARS],
            "disable_web_page_preview": True,
        }
        payload["reply_markup"] = keyboard or {"inline_keyboard": []}
        self._call("editMessageText", payload)

    def send_message(self, chat_id: int, text: str) -> int:
        for chunk in chunk_text(text) or [STRINGS["empty"]]:
            result = self._call(
                "sendMessage",
                {"chat_id": chat_id, "text": chunk, "disable_web_page_preview": True},
            )
            message_id = result.get("result", {}).get("message_id")
        return message_id

    def edit_message(self, chat_id: int, message_id: int, text: str) -> None:
        self._call(
            "editMessageText",
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text[:MAX_TELEGRAM_CHARS],
                "disable_web_page_preview": True,
            },
        )


def describe_permission(request: dict) -> str:
    action = request.get("action") or "?"
    resources = request.get("resources") or []
    shown = "\n".join(f"» {item}" for item in resources[:5]) or "» (sin detalle)"
    if action == "edit":
        return STRINGS["perm_edit"].format(what=shown)
    if action == "shell":
        return STRINGS["perm_shell"].format(what=shown)
    return STRINGS["perm_other"].format(action=action, what=shown)


class PermissionBroker:
    """Turns `permission.asked` events into Telegram buttons.

    Every request gets a short token so `callback_data` stays inside Telegram's
    64-byte limit regardless of how long the OpenCode request id is. A request
    that nobody answers is rejected after `ttl` seconds, so a turn can never hang
    forever waiting on a phone that walked away.
    """

    def __init__(
        self,
        oc: OpenCode,
        tg: Telegram,
        turns: dict[str, Turn],
        lock: threading.Lock,
        ttl: int = PERMISSION_TTL,
    ) -> None:
        self.oc = oc
        self.tg = tg
        self.turns = turns
        self.turns_lock = lock
        self.ttl = ttl
        self.pending: dict[str, dict] = {}
        self.lock = threading.Lock()
        self.counter = 0

    def _chat_for(self, session_id: str) -> int | None:
        with self.turns_lock:
            turn = self.turns.get(session_id)
        return turn.chat_id if turn else None

    def _keyboard(self, token: str) -> dict:
        return {
            "inline_keyboard": [
                [
                    {"text": f"✅ {STRINGS['perm_allow']}", "callback_data": f"{token}:once"},
                    {"text": f"❌ {STRINGS['perm_deny']}", "callback_data": f"{token}:reject"},
                ]
            ]
        }

    def ask(self, request: dict) -> None:
        session_id = request.get("sessionID") or ""
        request_id = request.get("id") or ""
        chat_id = self._chat_for(session_id)
        if chat_id is None or not request_id:
            log(f"permission {request_id}: nothing to ask in, rejecting")
            self._reply(session_id, request_id, "reject")
            return

        text = describe_permission(request)
        with self.lock:
            self.counter += 1
            token = f"p{self.counter}"
            entry = {
                "session_id": session_id,
                "request_id": request_id,
                "chat_id": chat_id,
                "message_id": None,
                "text": text,
            }
            self.pending[token] = entry
        try:
            entry["message_id"] = self.tg.send_keyboard(chat_id, text, self._keyboard(token))
        except Exception as exc:  # noqa: BLE001 - never leave the turn blocked
            log(f"permission prompt failed: {exc}")
            self._settle(token, "reject", "no se pudo preguntar")
            return

        timer = threading.Timer(self.ttl, self._expire, args=(token,))
        timer.daemon = True
        entry["timer"] = timer
        timer.start()

    def answer(self, token: str, decision: str) -> str:
        return self._settle(token, decision, None)

    def cancel_session(self, session_id: str) -> None:
        with self.lock:
            tokens = [t for t, e in self.pending.items() if e["session_id"] == session_id]
        for token in tokens:
            self._settle(token, "reject", "turno interrumpido")

    def _expire(self, token: str) -> None:
        self._settle(token, "reject", STRINGS["perm_expired"])

    def _settle(self, token: str, decision: str, reason: str | None) -> str:
        with self.lock:
            entry = self.pending.pop(token, None)
        if entry is None:
            return STRINGS["perm_settled"]

        timer = entry.get("timer")
        if timer is not None:
            timer.cancel()

        self._reply(entry["session_id"], entry["request_id"], decision)
        if decision == "reject":
            with self.turns_lock:
                turn = self.turns.get(entry["session_id"])
            if turn is not None:
                turn.rejected += 1

        label = STRINGS["perm_allowed"] if decision == "once" else STRINGS["perm_rejected"]
        if reason:
            label = f"{label} ({reason})"
        message_id = entry.get("message_id")
        if message_id:
            try:
                self.tg.edit_keyboard(entry["chat_id"], message_id, f"{entry['text']}\n\n{label}")
            except Exception as exc:  # noqa: BLE001
                log(f"permission settle edit failed: {exc}")
        return label

    def _reply(self, session_id: str, request_id: str, decision: str) -> None:
        if not session_id or not request_id:
            return
        try:
            self.oc.permission_reply(session_id, request_id, decision)
        except OpenCodeError as exc:
            log(f"permission reply failed: {exc}")


class Bridge:
    def __init__(self, oc: OpenCode, tg: Telegram, sessions_path: Path = SESSIONS_PATH) -> None:
        self.oc = oc
        self.tg = tg
        self.sessions_path = sessions_path
        self.sessions = self._load()
        self.turns: dict[str, Turn] = {}
        self.lock = threading.Lock()
        self.stream = EventStream(oc, self.turns, self.lock)
        self.broker = PermissionBroker(oc, tg, self.turns, self.lock)
        self.stream.on_permission = self.broker.ask
        self.allowed: set[int] = set()

    # -- state -----------------------------------------------------------
    def _load(self) -> dict:
        if not self.sessions_path.is_file():
            return {}
        try:
            return json.loads(self.sessions_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    def _save(self) -> None:
        tmp = self.sessions_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.sessions, indent=2), encoding="utf-8")
        tmp.replace(self.sessions_path)

    def session_for(self, chat_id: int) -> str:
        record = self.sessions.setdefault(str(chat_id), {})
        session_id = record.get("session")
        if not session_id:
            session_id = self.oc.create_session()
            record["session"] = session_id
            self._save()
        return session_id

    def reset_session(self, chat_id: int) -> None:
        self.sessions.pop(str(chat_id), None)
        self._save()

    # -- turn ------------------------------------------------------------
    def run_turn(self, chat_id: int, text: str) -> None:
        """Blocking turn execution. Safe to call from any thread."""
        session_id = self.session_for(chat_id)
        turn = Turn(session_id, chat_id)
        with self.lock:
            self.turns[session_id] = turn

        placeholder = self.tg.send_message(chat_id, STRINGS["thinking"])
        last_edit = 0.0
        prompt_id = ""

        try:
            prompt_id = self.oc.prompt(session_id, text)
            deadline = time.time() + TURN_TIMEOUT
            while not turn.done.is_set():
                if time.time() > deadline:
                    turn.error = f"timeout after {TURN_TIMEOUT}s"
                    break
                if turn.done.wait(0.4):
                    break
                now = time.time()
                preview = turn.preview()
                if (
                    now - last_edit >= PREVIEW_INTERVAL
                    and preview != turn.previewed
                    and len(preview) >= PREVIEW_MIN_CHARS
                ):
                    last_edit = now
                    turn.previewed = preview
                    try:
                        self.tg.edit_message(chat_id, placeholder, preview)
                    except Exception as exc:  # noqa: BLE001 - a failed preview must not end the turn
                        log(f"preview edit failed: {exc}")
        except OpenCodeError as exc:
            turn.error = str(exc)
            if "404" in str(exc) or "NotFound" in str(exc):
                self.reset_session(chat_id)
                self.deliver(chat_id, placeholder, STRINGS["gone"], "reset")
                return
        finally:
            with self.lock:
                self.turns.pop(session_id, None)

        if turn.error:
            self.deliver(chat_id, placeholder, f"Error: {turn.error}", "")
            return

        try:
            reply = self.oc.final_reply(session_id, prompt_id) if prompt_id else ""
        except OpenCodeError as exc:
            reply = ""
            log(f"final read failed: {exc}")
        if not reply:
            if turn.parts:
                reply = turn.preview()
            elif turn.aborted:
                reply = STRINGS["aborted"]
            elif turn.rejected:
                reply = STRINGS["perm_blocked"]
            else:
                reply = STRINGS["empty"]

        self.deliver(chat_id, placeholder, reply, self.footer(turn))

    def footer(self, turn: Turn) -> str:
        seconds = int(time.time() - turn.started)
        mark = " · abortado" if turn.aborted else ""
        return f"\n\n${turn.cost:.4f} · {seconds}s{mark}"

    def deliver(self, chat_id: int, message_id: int, text: str, footer: str) -> None:
        body = (text + footer).strip()
        chunks = chunk_text(body)
        if not chunks:
            chunks = [STRINGS["empty"]]
        try:
            self.tg.edit_message(chat_id, message_id, chunks[0])
        except Exception as exc:  # noqa: BLE001 - fall back to a fresh message
            log(f"final edit failed, sending instead: {exc}")
            self.tg.send_message(chat_id, chunks[0])
        for extra in chunks[1:]:
            self.tg.send_message(chat_id, extra)

    def handle_callback(self, update: dict) -> None:
        callback = update.get("callback_query") or {}
        callback_id = callback.get("id")
        data = callback.get("data") or ""
        user_id = (callback.get("from") or {}).get("id")
        if self.allowed and user_id not in self.allowed:
            log(f"rejected callback from user_id={user_id}")
            self.tg.answer_callback(callback_id, STRINGS["perm_not_yours"])
            return

        token, _, decision = data.partition(":")
        if decision not in ("once", "reject"):
            self.tg.answer_callback(callback_id, STRINGS["perm_settled"])
            return

        label = self.broker.answer(token, decision)
        self.tg.answer_callback(callback_id, label)

    def abort(self, chat_id: int) -> str:
        session_id = (self.sessions.get(str(chat_id)) or {}).get("session")
        with self.lock:
            turn = self.turns.get(session_id) if session_id else None
        if turn is None:
            return STRINGS["nothing_running"]
        turn.aborted = True
        self.broker.cancel_session(session_id)
        try:
            self.oc.interrupt(session_id)
        except OpenCodeError as exc:
            log(f"interrupt failed: {exc}")
        return STRINGS["aborted"]

    # -- transport -------------------------------------------------------
    def handle_update(self, update: dict) -> None:
        if "callback_query" in update:
            self.handle_callback(update)
            return

        message = update.get("message") or {}
        text = (message.get("text") or "").strip()
        chat_id = (message.get("chat") or {}).get("id")
        user_id = (message.get("from") or {}).get("id")
        if not text or chat_id is None or user_id is None:
            return

        if self.allowed and user_id not in self.allowed:
            log(f"rejected user_id={user_id}")
            self.tg.send_message(chat_id, STRINGS["unauthorized"].format(user_id=user_id))
            return

        if text in ("/start", "/help"):
            self.tg.send_message(chat_id, STRINGS["help"])
            return
        if text == "/id":
            self.tg.send_message(chat_id, f"user id: {user_id}\nchat id: {chat_id}")
            return
        if text == "/new":
            self.reset_session(chat_id)
            self.tg.send_message(chat_id, STRINGS["new_session"])
            return
        if text == "/abort":
            self.tg.send_message(chat_id, self.abort(chat_id))
            return
        if text == "/status":
            record = self.sessions.get(str(chat_id), {})
            self.tg.send_message(
                chat_id,
                "session: {session}\nagent: {agent}\nworkspace: {workspace}".format(
                    session=record.get("session", "sin sesión todavía"),
                    agent=self.oc.agent,
                    workspace=self.oc.workspace,
                ),
            )
            return

        with self.lock:
            busy = any(t.chat_id == chat_id for t in self.turns.values())
        if busy:
            self.tg.send_message(chat_id, STRINGS["busy"])
            return

        threading.Thread(
            target=self.run_turn,
            args=(chat_id, text),
            name=f"turn-{chat_id}",
            daemon=True,
        ).start()

    def poll_forever(self) -> None:
        self.stream.start()
        offset = None
        failures = 0
        while True:
            try:
                updates = self.tg.get_updates(offset)
                failures = 0
            except Exception as exc:  # noqa: BLE001 - keep serving through transient errors
                failures += 1
                log(f"poll failed ({failures}): {exc}")
                time.sleep(min(30, 2**failures))
                continue
            for update in updates:
                offset = update["update_id"] + 1
                try:
                    self.handle_update(update)
                except Exception as exc:  # noqa: BLE001
                    log(f"update failed: {exc}")


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
    try:
        info = oc.ping()
    except OpenCodeError as exc:
        log(f"cannot reach opencode at {base_url}: {exc}")
        return 1

    log(f"opencode {info.get('version')} at {base_url}, agent={agent}, workspace={workspace}")
    if not allowed:
        log("ALLOWED_USER_IDS is empty: every Telegram user is accepted")

    bridge = Bridge(oc, Telegram(token))
    bridge.allowed = allowed
    bridge.poll_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
