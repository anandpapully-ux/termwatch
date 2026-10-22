import { useEffect, useRef } from "react";

import { formatDuration, STATUS_LABELS } from "../lib/format";
import { sessionPath } from "../lib/router";
import { showNotification } from "../services/notifications";
import { ACTIVE_STATUSES, type Session, type SessionStatus } from "../types/api";

/**
 * Notifies when a session observed as active finishes while the dashboard is
 * open. Sessions that were already finished when first seen never notify.
 */
export function useCompletionNotifications(sessions: Map<string, Session>, loaded: boolean): void {
  const previous = useRef<Map<string, SessionStatus> | null>(null);

  useEffect(() => {
    if (!loaded) return;
    const before = previous.current;
    const now = new Map<string, SessionStatus>();
    sessions.forEach((session, id) => now.set(id, session.status));
    previous.current = now;
    if (before === null) return; // first snapshot: establish the baseline only

    sessions.forEach((session) => {
      const old = before.get(session.id);
      if (!old || !ACTIVE_STATUSES.includes(old) || ACTIVE_STATUSES.includes(session.status)) return;
      const runtime = session.ended_at ? formatDuration(session.ended_at - session.started_at) : null;
      const exit = session.exit_code !== null ? `exit code ${session.exit_code}` : null;
      const body = [runtime && `ran ${runtime}`, exit, session.last_line].filter(Boolean).join(" · ");
      void showNotification(
        `${session.name} — ${STATUS_LABELS[session.status]}`,
        body,
        sessionPath(session.id),
        `termwatch-${session.id}`,
      );
    });
  }, [sessions, loaded]);
}
