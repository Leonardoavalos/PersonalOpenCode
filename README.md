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

## The agent

`.opencode/agents/remote.md` defines the `remote` agent. **It is read-only on
purpose**: reads, glob, grep and web tools are allowed; `edit`, `shell` and
`subagent` are denied.

The reason is mechanical, not caution for its own sake. An `ask` permission
blocks the turn until someone answers the prompt, and this channel has no way to
answer yet. A read-only agent cannot block. Permission buttons arrive in step 3.
To make it write-capable before then, flip those three `deny` entries to `ask`
and answer the requests from a terminal running `opencode` in this directory.

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

Done: streaming output, `/abort`, per-turn cost, session continuity, allowlist,
long replies split at Telegram's 4096-character limit.

Not done: permission buttons, a second user, systemd. Those are steps 3 to 5 of
`SPEC.md`.

## Known cost of the global instructions

The `remote` agent replaces the base system prompt, but OpenCode still injects
the global instruction files (`~/.config/opencode/AGENTS.md`). Measured effect:
a three-sentence reply cost $0.011 with about 28k cached input tokens, because
the model reasons about the injected memory protocol before answering. Turns
land around 25 to 30 seconds for the same reason.

That overhead belongs to the channel, not to the question. Reducing it is a
configuration decision, not a code change; it is recorded in `SPEC.md` as an
open question.

## Security notes

- `opencode serve` binds `127.0.0.1` only. Do not change that.
- The server password is a real credential. It lives in `.env`, which is
  gitignored.
- An empty `ALLOWED_USER_IDS` accepts anyone who finds the bot. Set it before you
  share the bot name with anyone.
- The assistant reads files in `WORKSPACE`. Point it at a directory you are
  comfortable exposing to a model.
