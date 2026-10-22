import type { MouseEvent } from "react";

import { describeExit, formatDuration, formatTimestamp, sessionRuntime } from "../lib/format";
import { navigate, sessionPath } from "../lib/router";
import type { Session } from "../types/api";
import { StatusBadge } from "./StatusBadge";

export function SessionCard({ session, now }: { session: Session; now: number }) {
  const href = sessionPath(session.id);
  const open = (event: MouseEvent<HTMLAnchorElement>) => {
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.button !== 0) return;
    event.preventDefault();
    navigate(href);
  };
  const exit = describeExit(session);
  return (
    <a className={`session-card card-${session.status}`} href={href} onClick={open}>
      <div className="session-card-top">
        <h3 className="session-name">{session.name}</h3>
        <StatusBadge status={session.status} />
      </div>
      <div className="session-meta">
        <span>
          <span className="meta-label">Runtime</span> {formatDuration(sessionRuntime(session, now))}
        </span>
        <span>
          <span className="meta-label">Started</span> {formatTimestamp(session.started_at)}
        </span>
        {exit !== null && (
          <span>
            <span className="meta-label">Exit</span> {exit}
          </span>
        )}
      </div>
      <div className="session-last-line" title={session.last_line ?? undefined}>
        {session.last_line ?? <span className="muted">No output yet</span>}
      </div>
    </a>
  );
}
