# Installation

## Requirements

- Linux (tested on Ubuntu 24.04). macOS is untested; Windows is not supported in V1.
- Python 3.12+ (`python3 --version`).
- Node.js 20.19+ or 22.12+ with npm, only to build the dashboard.
- Optional but recommended: [Tailscale](https://tailscale.com/download) on the
  computer and the phone.

## Install

From the repository root:

```bash
npm --prefix frontend install
npm --prefix frontend run build      # writes backend/src/termwatch/web/

pipx install .                       # or: uv tool install .
termwatch init
```

`termwatch init` creates:

| Path | Contents |
|---|---|
| `~/.config/termwatch/config.toml` | commented configuration template |
| `~/.config/termwatch/credentials.json` | access and agent tokens (mode 0600) |
| `~/.local/share/termwatch/` | database directory (mode 0700) |

### Virtual environment instead of pipx

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Remember to `source .venv/bin/activate` in every terminal where you use
`termwatch`, or call `/path/to/termwatch/.venv/bin/termwatch`.

### Upgrading

```bash
git pull
npm --prefix frontend install && npm --prefix frontend run build
pipx install --force .
```

Restart `termwatch server` afterwards. Running `termwatch run` processes
reconnect automatically.

## Running the server

```bash
termwatch server                 # foreground; Ctrl+C to stop
termwatch server --port 9000     # other port
```

The server must be running for the dashboard to update; commands started while
it is down still run and deliver their output once it is up.

### As a systemd user service (optional)

To have the server start at login and restart on failure:

```bash
mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/termwatch.service <<'EOF'
[Unit]
Description=TermWatch server

[Service]
ExecStart=%h/.local/bin/termwatch server
Restart=on-failure

[Install]
WantedBy=default.target
EOF
systemctl --user daemon-reload
systemctl --user enable --now termwatch.service
journalctl --user -u termwatch -f          # logs (never contain tokens)
```

Adjust `ExecStart` if `termwatch` is not in `~/.local/bin` (`command -v termwatch`).

## Uninstall

```bash
pipx uninstall termwatch                  # or: uv tool uninstall termwatch
rm -rf ~/.config/termwatch ~/.local/share/termwatch   # deletes tokens, sessions and logs
```
