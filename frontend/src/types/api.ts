// Types mirroring backend/src/termwatch/schemas.py. Timestamps are UNIX epoch seconds.

export type SessionStatus =
  | "running"
  | "succeeded"
  | "failed"
  | "interrupted"
  | "stopped"
  | "disconnected";

export const ACTIVE_STATUSES: readonly SessionStatus[] = ["running", "disconnected"];

export interface Session {
  id: string;
  name: string;
  command: string[] | null;
  cwd: string | null;
  hostname: string | null;
  pid: number | null;
  status: SessionStatus;
  status_reason: string | null;
  exit_code: number | null;
  signal: string | null;
  error: string | null;
  created_at: number;
  started_at: number;
  ended_at: number | null;
  last_seen_at: number;
  last_output_at: number | null;
  last_line: string | null;
  last_seq: number;
  first_retained_seq: number;
  retained_bytes: number;
  total_bytes: number;
  log_truncated: boolean;
  /** Increases with every change; the higher revision is the newer snapshot. */
  revision: number;
}

export interface SessionList {
  sessions: Session[];
  server_time: number;
}

export interface LogChunk {
  seq: number;
  ts: number;
  data: string;
}

export interface LogPage {
  session_id: string;
  chunks: LogChunk[];
  first_retained_seq: number;
  last_seq: number;
  has_more_before: boolean;
  has_more_after: boolean;
  server_time: number;
}

export interface AuthCheck {
  scope: "viewer" | "agent";
  server_time: number;
}

export type ServerMessage =
  | { type: "hello"; server_time: number; version: string }
  | { type: "session"; session: Session }
  | { type: "sessions_deleted"; session_ids: string[] }
  | { type: "chunks"; session_id: string; chunks: LogChunk[]; first_retained_seq?: number }
  | { type: "ping"; server_time: number }
  | { type: "pong"; server_time: number }
  | { type: "error"; code: string; message: string; session_id?: string };
