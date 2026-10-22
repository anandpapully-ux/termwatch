# Troubleshooting

Start with `termwatch config` (effective settings and file locations) and the
server's own log output (the terminal running `termwatch server`, or
`journalctl --user -u termwatch` for the systemd service). Add
`--log-level debug` to the server to see every HTTP request.

## The CLI

**"Cannot reach the TermWatch server at http://127.0.0.1:8765"**
The server isn't running, or is on another port/address. Start it with
`termwatch server`. Your command keeps running; output is buffered and sent once
the server is reachable ("Reconnected … sending buffered output"). If the server
runs on a non-default port, either keep `server.json` discoverable (same
`TERMWATCH_HOME`) or pass `--server http://127.0.0.1:PORT`.

**"Output forwarding disabled: the server rejected the agent token"**
The CLI and server read different `credentials.json` files. Common causes:
different `TERMWATCH_HOME`/`XDG_CONFIG_HOME` in the two terminals, or
`termwatch token rotate --all` was run while the command was running. The
command keeps running locally; re-run it once fixed.

**"Could not deliver the final status"**
The server was unreachable for the whole `--flush-timeout` (15 s) after the
command exited. The session will show as *disconnected*. Increase the timeout
(`--flush-timeout 120`) for flaky setups.

**Output arrives in big bursts or only at the end**
The program buffers its own output. TermWatch runs it on a pseudo-terminal and
sets `PYTHONUNBUFFERED=1`, which fixes most cases. For others use the program's
line-buffering option, e.g. `stdbuf -oL your-command`, `grep --line-buffered`,
`python -u`.

**Shell syntax doesn't work (`|`, `&&`, `*`, `$VAR`)**
TermWatch does not use a shell. Wrap it: `termwatch run --name X -- bash -c 'make && make test'`.

**"... characters of output were dropped"**
The server was unreachable long enough for more than 16 M characters to pile up
in memory; the oldest part was discarded to protect the machine.

**Ctrl+C doesn't stop the program**
Some programs trap SIGINT. Press Ctrl+C again (SIGTERM), and a third time
(SIGKILL).

## The server

**"port 8765 on 127.0.0.1 is already in use"**
Another TermWatch (or other program) uses the port: `ss -ltnp | grep 8765`.
Stop it or use `--port`.

**"cannot bind to 100.x.y.z: no network interface has that address"**
Tailscale is not connected (`tailscale status`).

**"invalid configuration" / "unknown keys"**
Check `~/.config/termwatch/config.toml`; keys are listed in README §9.

**"credentials file ... is unreadable or corrupt"**
TermWatch refuses to silently replace it. Fix or delete the file, or run
`termwatch token rotate --all` to regenerate both tokens.

**The dashboard shows "TermWatch API is running … dashboard has not been built"**
Run `npm --prefix frontend install && npm --prefix frontend run build`
(then `pipx install --force .` if you installed with pipx).

**Database growing too large**
Lower `max_log_bytes_per_session` and/or `max_sessions` in the config and
restart the server; retention runs at startup. SQLite reuses freed pages; to
shrink the file itself, stop the server and run
`sqlite3 ~/.local/share/termwatch/termwatch.db 'VACUUM'`.

## The dashboard / phone

**"Can't reach the TermWatch server"**
Check, in order: `termwatch server` is running; Tailscale is connected on both
devices; `tailscale serve status` shows the mapping to `http://127.0.0.1:8765`;
you're opening the exact `https://….ts.net` address.

**Sign-in fails with "That token was not accepted"**
Copy the token again from `termwatch token show` (it may have been rotated).
Make sure no spaces were added when pasting.

**"Live connection lost — reconnecting" stays visible**
The page can load but the WebSocket can't connect. With a custom reverse proxy,
make sure it forwards WebSocket upgrades on `/api/ws` and preserves the `Host`
header. The dashboard retries with backoff and catches up automatically once
connected.

**Session stuck as "Disconnected"**
The CLI stopped reporting without a final status: it was killed with `SIGKILL`,
the machine slept or lost power, or the server was unreachable for the whole
flush timeout. TermWatch does not guess the outcome. If the process is still
alive and its CLI comes back, the status updates; otherwise the session is
removed after `max_session_age_days`.

**No notifications**
- They need HTTPS (`tailscale serve`) or localhost; plain `http://100.x.y.z`
  doesn't qualify.
- Tap "Alerts off" and allow notifications when the browser asks.
- They only fire while the dashboard is open. On iPhone, add the dashboard to
  the home screen first (iOS 16.4+ only allows notifications for home-screen
  apps), and expect iOS to suspend it in the background.

**Copy button says "Copy failed"**
Some browsers block clipboard access over plain HTTP. Use HTTPS
(`tailscale serve`), or select the text manually.
