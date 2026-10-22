# TermWatch

Monitor long-running terminal processes from your phone.

You start a slow job — a data pipeline, a model training run, a build, a PDF
parser — and then you have to leave your desk. TermWatch lets you check on it
from your phone: give the command a name, run it through `termwatch run`, and
open the dashboard anywhere on your private network or VPN.

```bash
termwatch run --name "PDF Parser" -- python parser.py
```

On your phone you see **PDF Parser · Running · 8m 32s · `Processed page 248`**,
the live output as new lines arrive, and the final status and exit code when it
finishes or fails.

TermWatch is local-first: one Python process, one SQLite file, no cloud account,
no third-party services.

> **V1 scope.** Commands must be *started* through `termwatch run`; TermWatch
> does not attach to terminal tabs or processes that are already running. The
> dashboard is **read-only**: it cannot type into a process, stop it, or run
> anything. Linux is the supported platform (macOS likely works; Windows does not
> yet). See [Limitations](#11-current-limitations-and-roadmap).

---

## Contents

1. [What it does](#1-what-it-does)
2. [Architecture](#2-architecture)
3. [Prerequisites and installation](#3-prerequisites-and-installation)
4. [Development setup](#4-development-setup)
5. [Building and serving the production dashboard](#5-building-and-serving-the-production-dashboard)
6. [Running and monitoring a command](#6-running-and-monitoring-a-command)
7. [Opening the dashboard on your phone](#7-opening-the-dashboard-on-your-phone)
8. [Authentication and token rotation](#8-authentication-and-token-rotation)
9. [Configuration and log retention](#9-configuration-and-log-retention)
10. [Troubleshooting](#10-troubleshooting)
11. [Current limitations and roadmap](#11-current-limitations-and-roadmap)

Further reading: [docs/architecture.md](docs/architecture.md) ·
[docs/installation.md](docs/installation.md) · [docs/security.md](docs/security.md) ·
[docs/troubleshooting.md](docs/troubleshooting.md) · [docs/api.md](docs/api.md)

---

## 1. What it does

- **`termwatch run --name NAME -- COMMAND`** runs your command exactly as before:
  same output in the same terminal, colours and progress bars intact, and the
  same exit code returned to your shell. In the background it streams the output
  to the TermWatch server.
- **`termwatch server`** stores session metadata and output, and serves a
  mobile-friendly dashboard with live updates over WebSockets.
- The **dashboard** lists running, succeeded, failed, interrupted, stopped and
  disconnected sessions with runtime, start time and latest output line; shows
  live output with search, copy and pause-autoscroll; and can show a browser
  notification when a session finishes while the dashboard is open.

TermWatch never invents progress percentages. If your program prints progress,
you will see it in the output and the "latest output" line; otherwise you see
runtime, latest output and status.

## 2. Architecture

```
 Terminal A                     Terminal B                          Phone / laptop browser
┌──────────────────────┐      ┌─────────────────────────────┐      ┌─────────────────────────┐
│ termwatch run        │ HTTP │ termwatch server (FastAPI)  │  WS  │ Dashboard (React)       │
│  ├─ child process    │─────▶│  ├─ REST API  /api/...      │◀────▶│  ├─ session list        │
│  │   (PTY, no shell) │ POST │  ├─ WebSocket /api/ws ──────┼─────▶│  ├─ live log viewer     │
│  ├─ echo to terminal │      │  ├─ SQLite: sessions + logs │ HTTPS│  └─ notifications       │
│  └─ forwarder thread │      │  └─ serves built dashboard  │◀─────│   (read-only token)     │
│     (buffer + retry) │      └─────────────────────────────┘      └─────────────────────────┘
└──────────────────────┘          binds 127.0.0.1:8765 by default
     agent token                    reach it from a phone via Tailscale (see §7)
```

Data flow for one line of output:

1. The child writes `Processed page 248\n` to its pseudo-terminal.
2. `termwatch run` copies the bytes to your terminal unchanged and appends the
   decoded text to an in-memory buffer (never blocking the child).
3. A forwarder thread batches the buffer every ~200 ms into numbered chunks and
   `POST`s them to `/api/sessions/{id}/chunks`, retrying with the same sequence
   numbers if the server is unreachable.
4. The server stores new chunks in SQLite (duplicates are ignored), trims old
   output beyond the retention limit, updates the session's "latest output", and
   pushes the chunk to every dashboard subscribed to that session.
5. The dashboard appends chunks whose sequence number is newer than the last one
   it has. After a reconnect it re-subscribes with that number and the server
   sends exactly what was missed.

More detail, including restart reconciliation, in
[docs/architecture.md](docs/architecture.md).

## 3. Prerequisites and installation

| Requirement | Version | Needed for |
|---|---|---|
| Linux | any recent distribution | running TermWatch (macOS untested, Windows unsupported in V1) |
| Python | 3.12 or newer | server and CLI |
| Node.js + npm | Node 20.19+ or 22.12+ | building the dashboard (not needed at runtime) |
| Tailscale (recommended) | any | opening the dashboard from your phone |

Install from a checkout of this repository. Run every command from the
repository root.

```bash
cd termwatch                          # the repository root

# 1. Build the dashboard (one-off; output goes into the Python package)
npm --prefix frontend install
npm --prefix frontend run build

# 2a. Install the `termwatch` command for your user (recommended)
pipx install .                        # or: uv tool install .

# 2b. ...or install into a virtual environment instead
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# 3. Create the config directory and access tokens
termwatch init
```

With 2a, `termwatch` is available in every terminal. With 2b, activate the
virtual environment (`source .venv/bin/activate`) in each terminal you use, or
call `.venv/bin/termwatch` directly. If you change the frontend later, rebuild it
and re-run `pipx install --force .` (editable installs pick it up immediately).

See [docs/installation.md](docs/installation.md) for running the server as a
systemd user service.

## 4. Development setup

Use two or three terminals:

```bash
# Terminal 1 — backend (keep running)
source .venv/bin/activate
termwatch server

# Terminal 2 — dashboard dev server with hot reload (keep running)
npm --prefix frontend run dev          # http://127.0.0.1:5173, proxies /api to :8765

# Terminal 3 — generate some sessions
source .venv/bin/activate
termwatch run --name "Demo" -- python -c "import time
for i in range(30): print('step', i, flush=True); time.sleep(1)"
termwatch token show                   # sign-in link; open it, then replace :8765 with :5173
```

Tests and checks:

```bash
source .venv/bin/activate
pytest                                 # backend + CLI tests (starts real servers and processes)
npm --prefix frontend test             # frontend unit tests (vitest)
npm --prefix frontend run build        # type-check + production build
```

## 5. Building and serving the production dashboard

```bash
npm --prefix frontend run build
```

Vite writes the dashboard to `backend/src/termwatch/web/`, inside the Python
package. `termwatch server` serves it at `/`, so the API, WebSocket and
dashboard share one URL (by default `http://127.0.0.1:8765`). If the build is
missing, the server still runs and `/` shows a page explaining how to build it.

## 6. Running and monitoring a command

**Terminal 1** — start the server and leave it running:

```bash
termwatch server
```

**Terminal 2, 3, …** — run commands through TermWatch:

```bash
termwatch run --name "PDF Parser" -- python parser.py
termwatch run --name "Model Training" -- python train.py --epochs 50
```

- Everything after `--` is the command, passed to the OS directly (no shell), so
  quoting works as usual. For shell features (pipes, `&&`), run a shell
  explicitly: `termwatch run --name "Build" -- bash -c "make && make test"`.
- `--name` is optional (defaults to the command line) and need not be unique;
  every run gets its own session ID.
- Output appears in your terminal as normal and in the dashboard within about
  a fifth of a second.
- `termwatch` exits with the command's exit code (`128 + N` if it was killed by
  signal N), so `termwatch run ... && next-step` works.
- **Ctrl+C** interrupts the command as usual; the session is marked
  *interrupted*. Press Ctrl+C again to send SIGTERM, a third time for SIGKILL.
- If the server is down or restarts, the command keeps running. Output is
  buffered (up to 16 M characters, oldest dropped first with a visible marker)
  and delivered when the server is back.
- Closing the dashboard has no effect on the command.

Useful options: `--hide-command` (don't send the command line or working
directory), `--quiet`, `--flush-timeout SECONDS`, `--server URL`. Run
`termwatch run --help` for details. `termwatch list` shows recent sessions in the
terminal.

Statuses:

| Status | Meaning |
|---|---|
| Running | the CLI is in contact and the command has not exited |
| Succeeded | exited with code 0 |
| Failed | non-zero exit code, killed by a signal TermWatch did not send, or could not start (127 = not found, 126 = not executable) |
| Interrupted | you pressed Ctrl+C in the TermWatch terminal |
| Stopped | TermWatch itself received SIGTERM/SIGHUP (e.g. the terminal was closed) and stopped the command |
| Disconnected | the server has not heard from the CLI for 30 s, or the server restarted; the outcome is unknown and updates if the CLI reconnects |

## 7. Opening the dashboard on your phone

The server listens on `127.0.0.1` only, so nothing else can reach it until you
choose how. **Recommended: Tailscale Serve** — it keeps TermWatch on localhost,
encrypts traffic with WireGuard, gives you HTTPS (required for browser
notifications), and is only reachable from devices in your tailnet.

1. Install Tailscale on the computer and on your phone, and sign in to the same
   tailnet on both: <https://tailscale.com/download>.
2. In the Tailscale admin console, enable **MagicDNS** and **HTTPS
   certificates** (DNS page).
3. On the computer (Terminal 1 keeps `termwatch server` running):

   ```bash
   tailscale serve --bg 8765
   tailscale serve status        # shows https://<machine>.<tailnet>.ts.net
   ```

   (This is the syntax of Tailscale 1.52 and later; on older versions see
   `tailscale serve --help`.)

4. Print a sign-in link for that URL as a QR code and scan it with the phone's
   camera:

   ```bash
   termwatch token show --url https://<machine>.<tailnet>.ts.net --qr
   ```

   (Or open the URL on the phone and paste the token from `termwatch token show`.)
5. Optional: "Add to Home Screen" from the browser's share menu, and tap
   **Alerts off → on** to allow notifications.

To stop sharing: `tailscale serve reset`. **Do not use `tailscale funnel`** — it
publishes the service to the public internet.

Alternatives (binding TermWatch to a non-loopback address, e.g. your Tailscale
IP or a LAN address, plus the firewall rules that must go with it) are in
[docs/security.md](docs/security.md#exposing-the-dashboard-beyond-localhost).
Never expose TermWatch directly to the public internet.

## 8. Authentication and token rotation

`termwatch init` (or the first `termwatch server`/`run`) generates two random
256-bit tokens and stores them in `~/.config/termwatch/credentials.json` with
mode `0600` (directory `0700`):

- **Access token** — read-only; this is what your phone uses.
- **Agent token** — used by `termwatch run` to upload output. It never leaves
  your machine, so a leaked phone token cannot inject fake output.

```bash
termwatch token show                                   # token + http://127.0.0.1:8765/#token=... link
termwatch token show --url https://box.tailnet.ts.net --qr
termwatch token rotate                                 # new access token; signed-in browsers are logged out within seconds
termwatch token rotate --all                           # also rotate the agent token (restart running `termwatch run`s)
```

The sign-in link carries the token in the URL fragment (`#token=...`), which
browsers never send to servers; the dashboard moves it into local storage and
removes it from the address bar. Treat the token and the link like a password.
No restart is needed after rotation. Details: [docs/security.md](docs/security.md).

## 9. Configuration and log retention

Configuration lives in `~/.config/termwatch/config.toml` (created by
`termwatch init`, every key commented out). Environment variables
`TERMWATCH_<KEY>` override the file; `termwatch server --host/--port` override
both. `termwatch config` prints the effective values.

| Key | Default | Meaning |
|---|---|---|
| `host` | `127.0.0.1` | interface to bind; see the security doc before changing |
| `port` | `8765` | port |
| `max_log_bytes_per_session` | `5242880` (5 MiB) | when a session's stored output exceeds this, the oldest output is deleted down to ~80% of the limit. The dashboard says when output was trimmed. |
| `max_sessions` | `500` | finished sessions beyond this count (oldest first) are deleted with their logs |
| `max_session_age_days` | `30` | finished sessions older than this are deleted; disconnected sessions are deleted after this long without contact |
| `heartbeat_timeout_seconds` | `30` | silence after which a running session is shown as disconnected |
| `max_request_bytes` | `2097152` | maximum HTTP request body |
| `cors_origins` | `[]` | extra browser origins allowed to call the API; wildcards rejected |

Retention runs at startup and every 10 minutes. Running sessions are never
deleted. Worst-case disk use is therefore roughly
`max_sessions × max_log_bytes_per_session` (2.5 GB with defaults; lower either
value to reduce it).

Other locations: data in `~/.local/share/termwatch/` (`termwatch.db`, and
`server.json` while the server runs). Set `TERMWATCH_HOME=/some/dir` to keep
config and data together in one directory (useful for a second, isolated
instance). `XDG_CONFIG_HOME` and `XDG_DATA_HOME` are respected.

## 10. Troubleshooting

| Symptom | Fix |
|---|---|
| `Cannot reach the TermWatch server at http://127.0.0.1:8765` | Start `termwatch server` in another terminal. The command keeps running and its output is delivered once the server is up. |
| `server rejected the agent token` | The server and CLI use different config directories. Make sure `TERMWATCH_HOME`/`XDG_CONFIG_HOME` match in both terminals. |
| `port 8765 ... already in use` | Another server is running (`termwatch list` works?) or pick `--port 8766` and run commands with `--server http://127.0.0.1:8766`. |
| Phone shows "Can't reach the TermWatch server" | Is Tailscale connected on both devices? Does `tailscale serve status` show the mapping? Is `termwatch server` still running? |
| "Notifications require HTTPS" | Use the `https://….ts.net` URL from `tailscale serve`; plain `http://100.x.y.z:8765` is not a secure context. |
| Output only appears when the program exits | The program buffers internally. TermWatch provides a TTY and sets `PYTHONUNBUFFERED=1`; for other tools use their line-buffer flag (e.g. `stdbuf -oL cmd`, `grep --line-buffered`). |
| Session stuck on "Disconnected" | The CLI was killed (e.g. `kill -9`, machine slept) before reporting. TermWatch never guesses the outcome; it will be removed by age-based retention. |
| Dashboard says "Sign in again" | The token was rotated. Run `termwatch token show`. |

More in [docs/troubleshooting.md](docs/troubleshooting.md).

## 11. Current limitations and roadmap

Limitations in V1:

- Only processes **started with `termwatch run`** are monitored. Existing
  terminal tabs, `tmux` panes and already-running PIDs cannot be attached to.
- The dashboard is **read-only**: no keyboard input, no stop/restart button.
  Interactive programs can still be used locally in the terminal.
- **Linux only** is supported and tested. The PTY code is POSIX and probably
  works on macOS; Windows needs a ConPTY backend.
- Notifications fire only while the dashboard is open (tab or home-screen app)
  and the browser lets it run; locked phones and backgrounded tabs may not
  receive them. They are not push notifications.
- The dashboard strips ANSI colours rather than rendering them.
- If the TermWatch CLI itself is killed with `SIGKILL`, the child loses its
  output terminal and the session stays *disconnected*.
- One user per server; no accounts or per-session permissions.

Planned next steps: tmux/existing-terminal attachment, Web Push notifications
that work with the browser closed, ANSI colour rendering, optional (opt-in,
separately authorised) remote stop, and macOS/Windows backends.

## License

MIT — see [LICENSE](LICENSE).
