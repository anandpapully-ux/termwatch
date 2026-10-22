# HTTP and WebSocket API

All endpoints are under `/api`. Interactive OpenAPI docs are served at
`/api/docs` (schema at `/api/openapi.json`). Timestamps are UNIX epoch seconds
(floats, UTC). Errors are JSON: `{"detail": "..."}`.

Authentication: `Authorization: Bearer <token>`.

- **viewer** — the access token (dashboard). Read-only.
- **agent** — the agent token (CLI). Everything a viewer can do, plus writes.

| Status | Meaning |
|---|---|
| 401 | missing or invalid token |
| 403 | viewer token used on an agent endpoint |
| 404 | session not found |
| 409 | conflicting request (session already finished differently; ID reused with another name) |
| 413 | request body larger than `max_request_bytes` |
| 422 | validation error |

## Endpoints

### `GET /api/health` — no auth

`{"status": "ok", "version": "0.1.0", "server_time": 1792671261.9}`

### `GET /api/auth/check` — viewer

`{"scope": "viewer" | "agent", "server_time": …}`

### `GET /api/sessions` — viewer

Query: `status` (repeatable: `running`, `succeeded`, `failed`, `interrupted`,
`stopped`, `disconnected`), `q` (substring of name, command or latest line),
`limit` (1–1000, default 200), `offset`.

Active sessions first, then newest first. Response:
`{"sessions": [Session, …], "server_time": …}`

### `GET /api/sessions/{id}` — viewer

Returns a `Session`:

```json
{
  "id": "4b5e6dff-becd-4475-aa88-91892bb05c96",
  "name": "PDF Parser",
  "command": ["python", "parser.py"],
  "cwd": "/home/me/project",
  "hostname": "workstation",
  "pid": 41230,
  "status": "running",
  "status_reason": null,
  "exit_code": null,
  "signal": null,
  "error": null,
  "created_at": 1792671261.9,
  "started_at": 1792671261.8,
  "ended_at": null,
  "last_seen_at": 1792671290.1,
  "last_output_at": 1792671290.1,
  "last_line": "Processed page 248",
  "last_seq": 811,
  "first_retained_seq": 0,
  "retained_bytes": 20412,
  "total_bytes": 20412,
  "log_truncated": false,
  "revision": 815
}
```

`command` and `cwd` are `null` for sessions started with `--hide-command`.
`revision` increases with every change; when two snapshots disagree, the higher
revision wins.

### `GET /api/sessions/{id}/logs` — viewer

Query (mutually exclusive):

- none — the newest chunks (tail);
- `after_seq=N` — oldest chunks with `seq > N` (forward paging; `-1` = from the start);
- `before_seq=N` — newest chunks with `seq < N` (backward paging).

`limit` 1–2000 (default 500); a page also stops at ~512 KB.

```json
{
  "session_id": "…",
  "chunks": [{"seq": 0, "ts": 1792671261.94, "data": "Processed page 1\r\n"}],
  "first_retained_seq": 0,
  "last_seq": 3,
  "has_more_before": false,
  "has_more_after": false,
  "server_time": …
}
```

`data` is raw terminal text (including `\r\n` line endings and ANSI escape
sequences). Chunks with `seq < first_retained_seq` were deleted by retention.

### `POST /api/sessions` — agent

```json
{"id": "<uuid4 chosen by the client>", "name": "PDF Parser", "command": ["python", "parser.py"],
 "cwd": "/home/me/project", "hostname": "workstation", "pid": 41230, "started_at": 1792671261.8}
```

`201` with the `Session` when created, `200` if a session with this ID and name
already exists (idempotent retry; a disconnected session becomes running again).

### `POST /api/sessions/{id}/chunks` — agent

```json
{"chunks": [{"seq": 0, "ts": 1792671261.94, "data": "Processed page 1\r\n"}]}
```

1–256 chunks, each ≤ 65,536 characters. Chunks whose `seq` was already stored
(or already trimmed) are ignored. Response:
`{"accepted": 1, "duplicates": 0, "last_seq": 0}`.

### `POST /api/sessions/{id}/heartbeat` — agent

Marks the CLI as alive. Returns the `Session`.

### `POST /api/sessions/{id}/finish` — agent

```json
{"status": "failed", "exit_code": 3, "signal": null, "ended_at": 1792671300.2, "error": null}
```

`status` is one of `succeeded` (requires `exit_code` 0), `failed`,
`interrupted`, `stopped`. Repeating the same outcome is a no-op; a different
outcome for a finished session is `409`.

## WebSocket `/api/ws`

The first frame must be `{"type": "auth", "token": "<access or agent token>"}`
within 10 s. Close codes: `4401` bad/missing/rotated token, `4403` foreign
`Origin`, `4408` client too slow.

Client → server:

| Frame | Effect |
|---|---|
| `{"type": "subscribe", "session_id": "…", "after_seq": N}` | receive that session's chunks with `seq > N`: stored ones first, then live, in order, without duplicates |
| `{"type": "unsubscribe", "session_id": "…"}` | stop receiving chunks |
| `{"type": "ping"}` | server answers `pong` |

Server → client:

| Frame | When |
|---|---|
| `{"type": "hello", "server_time": …, "version": …}` | after successful auth; re-fetch state on every hello |
| `{"type": "session", "session": Session}` | any session changed (sent to all clients) |
| `{"type": "sessions_deleted", "session_ids": […]}` | removed by retention |
| `{"type": "chunks", "session_id": …, "chunks": […], "first_retained_seq": N}` | output for a subscribed session |
| `{"type": "ping", "server_time": …}` | every 15 s |
| `{"type": "error", "code": …, "message": …}` | bad request, unknown session |
