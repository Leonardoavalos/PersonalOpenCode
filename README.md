# opencode-telegram

A small Telegram bridge for a personal OpenCode instance. Step 1 of the build:
send a message from your phone, get the assistant reply back in the chat.

Deliberately boring: Python standard library only, no dependencies, no build step,
no `node_modules`.

## What it does

```
Telegram  ->  bot.py  ->  opencode serve (127.0.0.1:4096)  ->  model
                     <-  reply text  <-                      <-
```

One OpenCode session per Telegram chat, remembered in `sessions.json`.

## Prerequisites

- Python 3.11 or newer (`python3 -V`)
- `opencode` on `PATH` (verified against 2.1.4)
- A bot token from [@BotFather](https://t.me/BotFather)

## Setup

1. Create the bot: talk to **@BotFather**, `/newbot`, copy the token.
2. Configure:

   ```sh
   cp env.example .env
   ```

3. Fill `.env`:
   - `TELEGRAM_BOT_TOKEN` — the token from BotFather.
   - `OPENCODE_PASSWORD` — any long random string. It authenticates the local
     server; the bot sends it as HTTP Basic with username `opencode`.
   - `ALLOWED_USER_IDS` — leave empty for the first run. The bot replies with
     your Telegram user id when it rejects you, so you can paste it here.
4. Run:

   ```sh
   ./run.sh
   ```

`run.sh` starts `opencode serve`, waits for it, then runs the bot. Ctrl+C stops
both.

## Commands

| Command | Effect |
| --- | --- |
| `/start` | help |
| `/new` | start a fresh session |
| `/status` | session id, agent, workspace |
| `/abort` | interrupt the turn in progress |
| `/id` | your Telegram user and chat ids |

Anything else is sent to the assistant as a prompt.

While a turn runs, the reply message is rewritten about once per second, so the
answer appears as it is generated. A footer with the turn cost and duration is
appended when it finishes.

## The agent and its permissions

`.opencode/agents/remote.md` defines the `remote` agent. Reads, glob, grep and
web tools are allowed. `edit` and `shell` are `ask`: the turn blocks and a
message appears in Telegram with ✅ Permitir / ❌ Rechazar.

- One tap approves **one** action. Approval is not remembered between turns.
- Nobody tapping is an answer: the bridge rejects the request after 120 seconds,
  so a turn can never hang forever on a phone that walked away.
- `/abort` rejects every pending request for that session before interrupting.
- Only allowlisted Telegram users can tap. A tapped button reports its outcome
  in a toast, and a stale tap says it was already resolved.

To make the agent fully autonomous, change `ask` to `allow` in the agent file —
and understand that you are then handing an unattended agent your filesystem.

## How a turn runs

`/api/event` is an SSE stream, consumed by one background thread and routed by
session id. Text arrives as `session.text.delta` and is rewritten into the same
Telegram message; reasoning deltas are dropped, not shown.

The final answer is always re-read from `/api/session/{id}/message` rather than
taken from the stream. If the stream drops, the live preview degrades but the
reply does not.

Telegram rate-limits edits, so the preview is throttled to one rewrite per 1.2
seconds and skipped when the text did not change.

## Scope

Done: streaming output, `/abort`, per-turn cost, agent approval buttons,
session continuity, allowlist, long replies split at Telegram's 4096-character
limit.

Not done: a second user, systemd. Those are steps 4 and 5 of `SPEC.md`.

## Config isolation

`run.sh` points `OPENCODE_CONFIG_DIR` at `bot-config/`, which holds an almost
empty `opencode.jsonc`. That keeps the owner's global OpenCode directory — a
146 KB `opencode.json` and a 16.8 KB `AGENTS.md` — out of every Telegram turn.

Measured on 2.1.4 with the same model, same agent, same one-word prompt, two runs
per case:

| | global config | isolated config |
| --- | --- | --- |
| latency | 11.5 – 11.7 s | 6.2 – 6.8 s |
| cost | $0.0039 – 0.0065 | $0.0016 – 0.0018 |
| cached input tokens | 10,496 | 3,072 |
| reasoning tokens | 150 – 412 | 32 – 53 |

The agent still loads from `.opencode/agents/remote.md` because discovery follows
the workspace, not the config dir. Providers and credentials are unaffected.

If you want a command, skill or MCP server available to the bot, put it under
`bot-config/` rather than in the global directory, or it will silently re-add the
same overhead.

## Security notes

- `opencode serve` binds `127.0.0.1` only. Do not change that.
- The server password is a real credential. It lives in `.env`, which is
  gitignored.
- An empty `ALLOWED_USER_IDS` accepts anyone who finds the bot. Set it before you
  share the bot name with anyone.
- The assistant reads files in `WORKSPACE`. Point it at a directory you are
  comfortable exposing to a model.
