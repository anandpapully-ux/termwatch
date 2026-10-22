# Architecture

TermWatch has three parts: the **CLI** that runs and watches a command, the
**server** (FastAPI + SQLite) that stores and fans out what the CLI reports, and
the **dashboard** (React) that displays it. They talk over HTTP and one
WebSocket; there are no other services.

```
termwatch run ──HTTP (agent token)──▶ FastAPI ──▶ SessionService ──▶ SQLite (sessions, log_chunks)
                                          │               │
                                          │               └──▶ Hub ──WebSocket (access token)──▶ dashboards
                                          └── serves the built dashboard (same origin)
```

## Backend modules (`backend/src/termwatch/`)

| Module | Responsibility |
|---|---|
| `cli.py` | Typer commands: `init`, `server`, `run`, `list`, `config`, `token show/rotate` |
| `runner.py` | Launches the child, copies output, handles signals, classifies the outcome |
| `spawn.py` | OS-specific launching. `PtySpawner` (Linux/macOS) and `PipeSpawner` fallback behind a `Spawner` protocol |
| `forwarder.py` | Background thread: bounded buffer, batching, sequence numbers, retries, heartbeats, final status |
| `client.py` | HTTPX client used by the CLI (`trust_env=False`, so proxies never see the token) |
| `api.py` | FastAPI app factory, REST routes, WebSocket endpoint, maintenance loop |
| `sessions.py` | Session lifecycle and business rules (single place that writes) |
| `log_store.py` | Bounded per-session output storage and paging |
| `realtime.py` | WebSocket fan-out hub with ordered, de-duplicated delivery |
| `models.py` / `database.py` | SQLAlchemy models; SQLite engine in WAL mode |
| `schemas.py` | Pydantic request/response models and validation limits |
| `auth.py` | Token generation, storage (0600), verification, rotation |
| `config.py` | Settings from defaults, `config.toml`, `TERMWATCH_*` env vars, CLI flags |
| `web.py` | Static dashboard serving, security headers, request-size limit |
| `server.py` | uvicorn launcher, port checks, `server.json` runtime file |

## The CLI path

1. `termwatch run` generates a UUID for the session (so creating it is
   idempotent and can be retried), then starts the command with
   `subprocess.Popen(argv)` — no shell — with stdout and stderr attached to a
   **pseudo-terminal**. The child believes it is writing to a terminal, so it
   line-buffers its output and keeps colours and progress bars. stdin stays
   connected to your real terminal.
2. An `OutputPump` thread reads the PTY, writes the raw bytes to your terminal
   immediately, decodes them as UTF-8 (incrementally, so split multi-byte
   characters are fine) and calls `forwarder.submit(text)`.
3. `submit` only appends to an in-memory deque. If the buffer exceeds
   16 M characters (server unreachable for a long time), the oldest text is
   dropped and replaced by a marker line saying how much was lost. The child is
   never slowed down by the network.
4. The `OutputForwarder` thread creates the session (retrying until it
   succeeds), then repeatedly takes ~200 ms worth of buffered text, splits it
   into chunks of ≤ 8 K characters with consecutive sequence numbers, and
   uploads them. A failed upload is retried with the *same* chunks and numbers,
   so the server can recognise duplicates. Exponential backoff (0.5 s → 10 s) and
   rate-limited warnings apply while the server is unreachable. When idle it
   sends a heartbeat every 10 s.
5. When the child exits, the runner classifies the outcome (see below), then
   gives the forwarder up to `--flush-timeout` seconds (default 15) to deliver
   the remaining output followed by the final status. Then it exits with the
   child's exit code.

### Outcome classification

| What happened | Status | Exit code returned |
|---|---|---|
| exit 0 | succeeded | 0 |
| exit N ≠ 0 | failed | N |
| killed by signal S that TermWatch did not send | failed (signal recorded) | 128+S |
| command not found / not executable | failed (error recorded) | 127 / 126 |
| you pressed Ctrl+C | interrupted | whatever the child returned (usually 130) |
| TermWatch received SIGTERM/SIGHUP | stopped | whatever the child returned (usually 143/129) |

Ctrl+C in a terminal is delivered by the kernel to the whole foreground process
group, which includes the child, so TermWatch does not forward it again (it
would deliver a second KeyboardInterrupt). It only forwards SIGINT itself when
it is not in the terminal's foreground group (e.g. `kill -INT <termwatch pid>`).
Repeated Ctrl+C escalates to SIGTERM and then SIGKILL.

## The server path

All writes go through `SessionService`, which holds a process-wide lock around
each write transaction. SQLite allows one writer anyway; serialising in-process
avoids lock-retry errors and guarantees that **WebSocket events are published in
commit order**. Reads run concurrently (WAL mode).

### Storage

- `sessions` — one row per run: name, command, cwd, host, pid, status,
  status reason, exit code, signal, error, timestamps, latest output line, the
  last 4 KB of output (to compute the latest line across chunk boundaries),
  sequence/byte counters and a `revision` that increases with every change.
- `log_chunks` — `(session_id, seq)` primary key, timestamp, size, text.

Inserting a chunk whose `(session_id, seq)` already exists is a no-op; that is
what makes CLI retries safe. Output is stored as text (UTF-8 with invalid bytes
replaced) including ANSI escape sequences, so future versions can render
colours.

### Retention

- **Per session:** when retained output exceeds `max_log_bytes_per_session`,
  the oldest chunks are deleted until it is ≤ 80% of the limit (the newest chunk
  is always kept). `first_retained_seq` and `log_truncated` tell clients output
  was dropped; re-sent chunks below `first_retained_seq` are ignored.
- **Across sessions:** at startup and every 10 minutes, finished sessions past
  `max_session_age_days` or beyond the newest `max_sessions` are deleted with
  their logs. Disconnected sessions are only deleted by age. Running sessions
  are never deleted.

### Liveness and restart reconciliation

- Any request from the CLI (create, chunks, heartbeat) updates `last_seen_at`.
- A maintenance task runs every few seconds. A `running` session silent for
  `heartbeat_timeout_seconds` (default 30) becomes `disconnected`, with a reason.
- On **server startup**, every session still marked `running` becomes
  `disconnected` ("server restarted"), because the server cannot know whether
  its process is still alive.
- If the CLI is in fact alive, its next request (≤ 10 s) turns the session back
  into `running`, and buffered output is delivered. Its final report then sets
  the real outcome.
- The server **never** sets `succeeded`, `failed`, `interrupted` or `stopped`
  on its own. Only the CLI's final report does. A session whose CLI vanished
  stays `disconnected` until retention removes it.

### Live updates (`/api/ws`)

1. The browser connects and sends `{"type":"auth","token":"…"}` as its first
   frame (browsers cannot set headers on WebSockets, and tokens in URLs end up
   in logs). The server checks the `Origin` header against the `Host` header
   (or `cors_origins`) and closes with code 4401 on a bad token.
2. Every authenticated client receives `session` events for all sessions.
3. A client viewing a session sends `{"type":"subscribe","session_id":…,"after_seq":N}`.
   The hub registers the subscription in a *backfilling* state, reads chunks
   after `N` from SQLite page by page and sends them. Live chunks that arrive
   meanwhile are parked. When the backlog is exhausted the parked chunks newer
   than the last one sent are flushed and the subscription goes live. Result:
   per subscription, chunks arrive in strictly increasing order with no
   duplicates.
4. Each client has a bounded send queue. A client that cannot keep up is
   disconnected (code 4408) and recovers by reconnecting, rather than making the
   server buffer without limit.
5. The server sends `ping` every 15 s; the browser reconnects if it hears
   nothing for 40 s (handles phones that sleep or switch networks), and
   immediately when the tab becomes visible again or the network comes back.
6. When the credentials file changes (`termwatch token rotate`), connections
   authenticated with a token that is no longer valid are closed within a few
   seconds.

## The dashboard (`frontend/src/`)

- `services/` — the only code that talks to the backend: `api.ts` (typed REST),
  `liveSocket.ts` (WebSocket with backoff, watchdog and re-subscription),
  `auth.ts` (token storage), `notifications.ts`.
- `hooks/useSessions.tsx` — session list context. Loads via REST, applies
  pushed updates, re-fetches after every reconnect. Snapshots with a lower
  `revision` than the one held are ignored, so a slow REST response cannot
  overwrite a newer pushed status.
- `hooks/useSessionLogs.ts` — loads the latest output via REST, subscribes
  with the last sequence number, de-duplicates by sequence number
  (`lib/chunks.ts`), assembles lines (`lib/terminalText.ts`: CRLF, carriage-return
  progress bars, backspaces, ANSI stripping, all stateful across chunk
  boundaries) and renders at most once per animation frame.
- `components/LogViewer.tsx` — renders the newest 2,000 lines (expandable),
  search across all loaded lines, copy, pause/follow scrolling.
- Runtimes are computed from server time (clock offset measured on every
  response and ping), so a phone with a wrong clock still shows correct runtimes.

## Extension points

- **New process sources (tmux, existing terminals):** implement something that
  yields an output file descriptor and an exit status, i.e. a new `Spawner`, or
  feed `OutputForwarder.submit()` from another reader (e.g. `tmux pipe-pane`).
  The server only cares about the session/chunk/finish API.
- **Other operating systems:** add a `Spawner` (e.g. Windows ConPTY) and select
  it in `default_spawner()`; signal handling in `runner.py` is the other
  OS-specific piece.
- **Push notifications:** the hub already emits status transitions; a Web Push
  sender would subscribe to the same events.
- **Remote control:** deliberately absent. If added, it should use a separate,
  opt-in token scope, a confirmation step, and a dedicated endpoint per action
  (never arbitrary command execution).
