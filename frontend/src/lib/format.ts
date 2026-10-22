import type { Session, SessionStatus } from "../types/api";

export function formatDuration(totalSeconds: number): string {
  const seconds = Math.max(0, Math.floor(totalSeconds));
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;
  const pad = (n: number) => String(n).padStart(2, "0");
  if (h > 0) return `${h}h ${pad(m)}m ${pad(s)}s`;
  if (m > 0) return `${m}m ${pad(s)}s`;
  return `${s}s`;
}

/** Runtime so far (running) or total runtime (finished), in seconds. */
export function sessionRuntime(session: Session, nowSeconds: number): number {
  const end = session.ended_at ?? nowSeconds;
  return Math.max(0, end - session.started_at);
}

export function formatTimestamp(epochSeconds: number, now = Date.now()): string {
  const date = new Date(epochSeconds * 1000);
  const today = new Date(now);
  const time = date.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  if (date.toDateString() === today.toDateString()) return time;
  const day = date.toLocaleDateString(undefined, { month: "short", day: "numeric" });
  return `${day}, ${time}`;
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KiB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MiB`;
}

export const STATUS_LABELS: Record<SessionStatus, string> = {
  running: "Running",
  succeeded: "Succeeded",
  failed: "Failed",
  interrupted: "Interrupted",
  stopped: "Stopped",
  disconnected: "Disconnected",
};

export function describeExit(session: Session): string | null {
  if (session.exit_code === null) return null;
  return session.signal ? `${session.exit_code} (${session.signal})` : String(session.exit_code);
}
