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
| `/id` | your Telegram user and chat ids |

Anything else is sent to the assistant as a prompt.

## The agent

`.opencode/agents/remote.md` defines the `remote` agent. **It is read-only on
purpose**: reads, glob, grep and web tools are allowed; `edit`, `shell` and
`subagent` are denied.

The reason is mechanical, not caution for its own sake. An `ask` permission
blocks the turn until someone answers the prompt, and this channel has no way to
answer yet. A read-only agent cannot block. Permission buttons arrive in step 3.
To make it write-capable before then, flip those three `deny` entries to `ask`
and answer the requests from a terminal running `opencode` in this directory.

## Scope of this step

Working: text in, reply out, session continuity, allowlist, long replies split
at Telegram's 4096-character limit.

Not here yet: streaming output, a "working..." indicator, permission buttons,
per-turn cost display, systemd. Those are steps 2 to 4.

## Security notes

- `opencode serve` binds `127.0.0.1` only. Do not change that.
- The server password is a real credential. It lives in `.env`, which is
  gitignored.
- An empty `ALLOWED_USER_IDS` accepts anyone who finds the bot. Set it before you
  share the bot name with anyone.
- The assistant reads files in `WORKSPACE`. Point it at a directory you are
  comfortable exposing to a model.
