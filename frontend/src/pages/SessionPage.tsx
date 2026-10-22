import { useEffect, useState, type ReactNode } from "react";

import { AppHeader } from "../components/AppHeader";
import { ConnectionBanner } from "../components/ConnectionBanner";
import { LogViewer } from "../components/LogViewer";
import { StatusBadge } from "../components/StatusBadge";
import { useServerNow } from "../hooks/useNow";
import { useSessionLogs } from "../hooks/useSessionLogs";
import { useSessions } from "../hooks/useSessions";
import { describeExit, formatBytes, formatDuration, formatTimestamp, sessionRuntime } from "../lib/format";
import { ApiError, api } from "../services/api";
import type { Session } from "../types/api";

function useSession(id: string): { session: Session | undefined; error: ApiError | null } {
  const { sessions, loaded, upsert } = useSessions();
  const session = sessions.get(id);
  const [error, setError] = useState<ApiError | null>(null);
  useEffect(() => {
    if (!loaded || session) return;
    let cancelled = false;
    api
      .getSession(id)
      .then((found) => {
        if (!cancelled) upsert(found);
      })
      .catch((caught: unknown) => {
        if (!cancelled) setError(caught instanceof ApiError ? caught : new ApiError(0, String(caught)));
      });
    return () => {
      cancelled = true;
    };
  }, [id, loaded, session, upsert]);
  return { session, error };
}

function Detail({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="detail">
      <dt>{label}</dt>
      <dd>{children}</dd>
    </div>
  );
}

export function SessionPage({ id }: { id: string }) {
  const { session, error } = useSession(id);
  const logs = useSessionLogs(id);
  const now = useServerNow();

  if (!session) {
    return (
      <>
        <AppHeader back />
        <main className="page">
          {error ? (
            <div className="banner banner-error" role="alert">
              {error.status === 404 ? "This session no longer exists (it may have been removed by retention)." : error.message}
            </div>
          ) : (
            <div className="placeholder">Loading session…</div>
          )}
        </main>
      </>
    );
  }

  const exit = describeExit(session);
  const runtime = formatDuration(sessionRuntime(session, now));
  const trimmedNotice =
    session.log_truncated && logs.firstRetainedSeq > 0 ? (
      <div className="banner banner-info small">
        Older output was discarded by the per-session log limit; {formatBytes(session.total_bytes)} were produced in
        total.
      </div>
    ) : null;

  return (
    <>
      <AppHeader back />
      <ConnectionBanner />
      <main className="page session-page">
        <section className="session-summary">
          <div className="session-title-row">
            <h1 className="session-title">{session.name}</h1>
            <StatusBadge status={session.status} />
          </div>
          {session.status === "disconnected" && (
            <div className="banner banner-warning" role="status">
              <strong>Connection to this process was lost</strong> (last contact {formatTimestamp(session.last_seen_at)}).{" "}
              {session.status_reason ?? "The process may still be running."}
            </div>
          )}
          {session.error && <div className="banner banner-error">{session.error}</div>}
          <dl className="details">
            <Detail label={session.status === "disconnected" ? "Runtime (unconfirmed)" : "Runtime"}>{runtime}</Detail>
            <Detail label="Started">{formatTimestamp(session.started_at)}</Detail>
            {session.ended_at !== null && <Detail label="Ended">{formatTimestamp(session.ended_at)}</Detail>}
            {exit !== null && <Detail label="Exit code">{exit}</Detail>}
            {session.last_output_at !== null && (
              <Detail label="Last output">{formatTimestamp(session.last_output_at)}</Detail>
            )}
          </dl>
          <details className="more-details">
            <summary>Command &amp; host</summary>
            <dl className="details">
              <Detail label="Command">
                {session.command ? <code className="wrap">{session.command.join(" ")}</code> : <span className="muted">hidden</span>}
              </Detail>
              {session.cwd && (
                <Detail label="Directory">
                  <code className="wrap">{session.cwd}</code>
                </Detail>
              )}
              {session.hostname && <Detail label="Host">{session.hostname}</Detail>}
              {session.pid !== null && <Detail label="PID">{session.pid}</Detail>}
              <Detail label="Output stored">{formatBytes(session.retained_bytes)}</Detail>
              <Detail label="Session ID">
                <code className="wrap">{session.id}</code>
              </Detail>
            </dl>
          </details>
        </section>
        {logs.error && (
          <div className="banner banner-error" role="alert">
            {logs.error.message}
          </div>
        )}
        <LogViewer
          lines={logs.lines}
          loading={logs.loading}
          hasEarlier={logs.hasEarlier}
          loadingEarlier={logs.loadingEarlier}
          onLoadEarlier={() => void logs.loadEarlier()}
          notice={trimmedNotice}
        />
      </main>
    </>
  );
}
