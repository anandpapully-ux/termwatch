# Security

Terminal output often contains secrets: file paths, hostnames, tokens printed
by mistake, customer data, stack traces. Treat the TermWatch dashboard like an
SSH session into your machine's output, and read this page before making it
reachable from another device.

## Security model in one paragraph

TermWatch is a single-user tool. The server binds to `127.0.0.1` by default, so
only processes on your computer can reach it. Every API call except
`/api/health` needs a bearer token. The dashboard can only **read** session
metadata and output: there is no endpoint that runs a command, sends input to a
process, or stops one. Reaching it from your phone is done over a private
network you control — ideally Tailscale with `tailscale serve` — never the
public internet. A token is **not** a substitute for that network boundary.

## What is protected, and how

| Measure | Details |
|---|---|
| Loopback by default | `host = "127.0.0.1"`. Binding elsewhere prints a warning. |
| Two token scopes | *Access token* (read-only, for browsers) and *agent token* (uploads, used by `termwatch run` on the same machine). A stolen phone token cannot create sessions or inject output (`403`). |
| Strong tokens | 32 random bytes from `secrets.token_urlsafe` (256 bits). Compared in constant time. |
| Private storage | `~/.config/termwatch/credentials.json` is written atomically with mode `0600`; the directory is `0700`; the database and its WAL files are `0600`. Looser permissions are tightened on load. |
| No tokens in URLs or logs | The REST API uses the `Authorization` header. The WebSocket sends the token in its first message, not the URL. Sign-in links put it in the `#fragment`, which browsers never transmit; the dashboard removes it from the address bar. Logs record only the path and client address of rejected requests. |
| CLI never leaks via proxies | The CLI's HTTP client ignores `HTTP(S)_PROXY` variables. |
| No environment upload | The CLI sends the command line, working directory, host name and PID — never environment variables. `--hide-command` omits the command line and directory too. |
| Read-only dashboard | No command execution, no input, no process control. The CLI does not listen on any port. |
| Input validation | Session IDs must be UUIDs; names ≤ 200 chars with control characters removed; ≤ 256 command arguments; chunks ≤ 64 K characters, ≤ 256 per request; request bodies ≤ 2 MiB (configurable); unknown fields rejected. |
| Bounded storage | Per-session output limit and session count/age limits (see README §9). |
| Browser hardening | Strict Content-Security-Policy (`script-src 'self'`, no inline scripts, no third-party origins), `X-Frame-Options: DENY`, `frame-ancestors 'none'`, `Referrer-Policy: no-referrer`, `nosniff`. |
| CORS | No cross-origin access by default (the dashboard is same-origin). `cors_origins` must list explicit origins; `*` is rejected. Credentials are never allowed cross-origin. |
| WebSocket origin check | Connections whose `Origin` does not match the `Host` (or an allowed origin) are refused. |
| Rotation | `termwatch token rotate` takes effect immediately; open dashboards using the old token are disconnected within seconds. |

## What is *not* protected

- **Anyone with the access token can read all output** of all sessions until
  you rotate it. Don't paste sign-in links into chats or screenshots.
- **Plain HTTP is not encrypted.** If you bind TermWatch to a LAN address and
  browse to `http://192.168.x.y:8765`, the token and your output cross the
  network in clear text. Tailscale encrypts traffic between devices (WireGuard),
  and `tailscale serve` additionally provides HTTPS.
- **Other local users and malware.** A process running as your user can read
  `credentials.json` and the database. Root can read everything. TermWatch does
  not defend against a compromised account.
- **No rate limiting or lockout.** Guessing a 256-bit token is infeasible, but a
  server exposed to the internet would still be probed, logged and attacked for
  vulnerabilities in the stack. Do not expose it.
- **The dashboard stores the token in the browser's local storage.** Anyone who
  can unlock your phone and open the browser can read your output. Use "Sign
  out" on shared devices.
- **Output you print is stored** in the database until retention removes it.

## Exposing the dashboard beyond localhost

### Recommended: Tailscale Serve (no change to TermWatch's bind address)

```bash
termwatch server                     # stays on 127.0.0.1:8765
tailscale serve --bg 8765            # https://<machine>.<tailnet>.ts.net -> 127.0.0.1:8765
tailscale serve status
tailscale serve reset                # stop sharing
```

Only devices logged in to your tailnet can connect (and you can restrict it
further with Tailscale ACLs). You get HTTPS with a valid certificate, which
browsers require for notifications. Requests arrive at TermWatch from
`127.0.0.1`; authentication is still required.

**Never run `tailscale funnel`** for TermWatch: Funnel publishes the service on
the public internet.

### Binding to the Tailscale interface directly

If you cannot use Serve, bind to your machine's Tailscale IP only (not
`0.0.0.0`):

```bash
termwatch server --host "$(tailscale ip -4)"
```

Then open `http://100.x.y.z:8765` on the phone. Traffic is encrypted by
WireGuard, but the page is plain HTTP, so notifications and the async clipboard
API are unavailable. `termwatch run` finds the server through the
`server.json` file the server writes, so no extra flags are needed.

### Binding to a LAN address (least preferred)

Only on a network you fully trust (your home network, not a café or office
Wi-Fi). Traffic is **unencrypted**.

```bash
termwatch server --host 192.168.1.20      # your machine's LAN address, never 0.0.0.0 unless you must
```

Firewall the port so only the LAN can reach it, e.g. with `ufw`:

```bash
sudo ufw default deny incoming
sudo ufw allow from 192.168.1.0/24 to any port 8765 proto tcp
sudo ufw enable
sudo ufw status verbose
```

For the Tailscale-IP option the equivalent rule is
`sudo ufw allow in on tailscale0 to any port 8765 proto tcp`.

Never forward the port on your router, and never run TermWatch on a host with a
public IP without a firewall rule restricting the port.

### Reverse proxies

If you put another reverse proxy in front, terminate HTTPS there, keep
TermWatch bound to `127.0.0.1`, forward WebSocket upgrades for `/api/ws`, and
preserve the `Host` header (or add the public origin to `cors_origins`). Add
the proxy's own authentication if it is reachable by anyone but you.

## Deployment assumptions

- One trusted user owns the machine account that runs TermWatch.
- The CLI and server run on the same machine (V1).
- Remote access goes over a private, encrypted network you control.
- The phone or laptop viewing the dashboard is trusted while signed in.

If any of these do not hold, don't expose TermWatch beyond localhost.

## Reporting

If you find a security problem, please report it privately to the maintainers
rather than opening a public issue.
