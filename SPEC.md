# Spec — Personal OpenCode remote over Telegram

Status: phase 1 of 5 implemented. Phases 2 to 5 specified, not built.

## Problem

OpenCode runs on a workstation. The owner wants to use it from a phone, without a
terminal, and without exposing the machine to the internet. A second person (the
owner's partner) should be able to use it, but must not gain administrative
control.

## Goals

- Reach a personal OpenCode instance from a phone over Telegram.
- Keep the server bound to loopback. No tunnel, no public port, no webhook.
- One allowlisted secondary user, with a skill authored by the owner.
- Every privileged action stays under the owner's decision.

## Non-goals

- Multi-tenant or public bot. The allowlist is the product boundary.
- A web UI. Telegram is the only interface.
- Editing agent configuration from chat. Configuration is files plus `/reload`.
- Distribution. This is a personal tool.

## Architecture

```
Telegram  <->  bot.py  <->  opencode serve (127.0.0.1)  <->  model provider
                  |
                  +-> sessions.json   (chat id -> session id)
```

- `opencode serve` owns sessions, agents, permissions and tool execution.
- `bot.py` owns transport only: it authenticates, maps chat to session, sends the
  prompt, waits for the turn's idle marker, and chunks the reply.
- The agent (`.opencode/agents/remote.md`) owns behavior and policy.

Three places, three responsibilities. Do not let the bridge reimplement agent
policy.

## Constraints

- Host is WSL2 with no node, bun, npm or pnpm. Python 3.14 is the only runtime.
  Therefore: standard library only, no build step, no dependency tree.
- `opencode serve` binds `127.0.0.1`. This is a requirement, not a default.
- Telegram caps a message at 4096 characters and rate-limits edits.

## Security model

- Server authentication is HTTP Basic, username `opencode`, password from
  `OPENCODE_PASSWORD`.
- Telegram user ids are the only identity check. Empty `ALLOWED_USER_IDS` accepts
  everyone and exists only for first-run onboarding.
- Phase 1 authorizes nothing destructive: the agent denies `edit`, `shell` and
  `subagent`. It cannot block on a permission request because it can never file
  one.
- The model reads files under `WORKSPACE`. Treat that directory as disclosed to a
  third-party provider.

## Phases

### Phase 1 — end-to-end skeleton (done)

Acceptance: a Telegram message produces an assistant reply; sessions persist
across restarts; a rejected user receives their own id; replies over 4000
characters split cleanly; the process leaves no orphan server.

### Phase 2 — usable

Acceptance: the reply appears and updates while the turn runs; `/abort`
interrupts; per-turn cost is reported; a slow turn is visible within 2 seconds.

Known work: subscribe to `/api/event` instead of polling `/message`, edit one
Telegram message at most once per second, handle flood control.

### Phase 3 — safe

Acceptance: a permission request surfaces in Telegram with allow and deny
buttons; the decision reaches `/api/session/{id}/permission/{requestID}/reply`;
`edit` and `shell` move from `deny` to `ask`; an unanswered request expires
instead of hanging forever.

### Phase 4 — shared

Acceptance: the second allowlisted user gets a separate session, a separate
workspace, and a skill authored by the owner. They cannot see the owner's
sessions, cost, or configuration.

### Phase 5 — durable

Acceptance: survives logout and reboot; restarts on failure; logs rotate; a
crash does not lose the session map.

Deliberately last: until phases 2 and 3 exist, running it 24/7 adds risk without
adding value.

## Verified API facts

Measured against OpenCode 2.1.4, not assumed from documentation.

- `opencode serve` requires HTTP Basic auth: username `opencode`, password from
  `OPENCODE_PASSWORD`. Bearer tokens are rejected with 401.
- Setting `OPENCODE_PASSWORD` also suppresses the password echo at startup.
- Every route wraps its payload in `{"data": ...}`. Message lists add `cursor`;
  the agent list adds `location`.
- `GET /api/session/{id}/message` returns messages **newest first**.
- A turn is finished when a message of type `idle` exists newer than the prompt
  message. Assistant text lives in `content[]` items where `type == "text"`.
- `GET /api/agent` returns an empty list even with a `?directory=` parameter,
  because location resolution uses headers rather than query parameters. Confirm
  the active agent from the assistant message's `agent` field instead.
- Writing `.env.example` is blocked by the runtime's environment-file policy;
  `env.example` is the equivalent file.

## Open questions

- Which model should the phone channel use? Cost per turn dominates perceived
  quality on short messages.
- Does the secondary user get her own workspace directory or a shared one?
- Should `/new` also rotate the workspace?
