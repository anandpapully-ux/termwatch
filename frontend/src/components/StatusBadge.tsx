import { STATUS_LABELS } from "../lib/format";
import type { SessionStatus } from "../types/api";

// Each status has a distinct shape as well as a colour, so it reads without colour vision.
const ICONS: Record<SessionStatus, string> = {
  running: "●",
  succeeded: "✓",
  failed: "✕",
  interrupted: "‖",
  stopped: "■",
  disconnected: "?",
};

export function StatusBadge({ status }: { status: SessionStatus }) {
  return (
    <span className={`status-badge status-${status}`}>
      <span className="status-icon" aria-hidden="true">
        {ICONS[status]}
      </span>
      {STATUS_LABELS[status]}
    </span>
  );
}
