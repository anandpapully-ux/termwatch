import { useMemo, useState } from "react";

import { AppHeader } from "../components/AppHeader";
import { ConnectionBanner } from "../components/ConnectionBanner";
import { EmptyState } from "../components/EmptyState";
import { SessionCard } from "../components/SessionCard";
import { useServerNow } from "../hooks/useNow";
import { useSessions } from "../hooks/useSessions";
import { ACTIVE_STATUSES, type Session, type SessionStatus } from "../types/api";

type Filter = "all" | "active" | SessionStatus;

const FILTERS: { key: Filter; label: string; statuses: readonly SessionStatus[] | null }[] = [
  { key: "all", label: "All", statuses: null },
  { key: "active", label: "Active", statuses: ACTIVE_STATUSES },
  { key: "succeeded", label: "Succeeded", statuses: ["succeeded"] },
  { key: "failed", label: "Failed", statuses: ["failed"] },
  { key: "interrupted", label: "Interrupted", statuses: ["interrupted", "stopped"] },
  { key: "disconnected", label: "Disconnected", statuses: ["disconnected"] },
];

function matchesQuery(session: Session, needle: string): boolean {
  if (!needle) return true;
  const haystack = [session.name, session.last_line ?? "", session.command?.join(" ") ?? "", session.hostname ?? ""]
    .join("\n")
    .toLowerCase();
  return haystack.includes(needle);
}

export function OverviewPage() {
  const { sessions, loaded, error, refresh } = useSessions();
  const now = useServerNow();
  const [filter, setFilter] = useState<Filter>("all");
  const [query, setQuery] = useState("");

  const all = useMemo(() => [...sessions.values()].sort((a, b) => b.started_at - a.started_at), [sessions]);
  const needle = query.trim().toLowerCase();

  const counts = useMemo(() => {
    const result = new Map<Filter, number>();
    for (const option of FILTERS) {
      result.set(option.key, option.statuses ? all.filter((s) => option.statuses!.includes(s.status)).length : all.length);
    }
    return result;
  }, [all]);

  const selected = FILTERS.find((option) => option.key === filter)?.statuses ?? null;
  const filtered = all.filter((s) => (!selected || selected.includes(s.status)) && matchesQuery(s, needle));
  const active = filtered.filter((s) => ACTIVE_STATUSES.includes(s.status));
  const finished = filtered.filter((s) => !ACTIVE_STATUSES.includes(s.status));

  return (
    <>
      <AppHeader>
        <div className="toolbar">
          <input
            type="search"
            className="search-input"
            placeholder="Search name, command or output"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            aria-label="Search sessions"
            autoCapitalize="off"
            autoCorrect="off"
          />
          <div className="filter-chips" role="tablist" aria-label="Filter by status">
            {FILTERS.map((option) => (
              <button
                key={option.key}
                type="button"
                role="tab"
                aria-selected={filter === option.key}
                className={`chip ${filter === option.key ? "chip-selected" : ""}`}
                onClick={() => setFilter(option.key)}
              >
                {option.label}
                <span className="chip-count">{counts.get(option.key) ?? 0}</span>
              </button>
            ))}
          </div>
        </div>
      </AppHeader>
      <ConnectionBanner />
      <main className="page">
        {error && (
          <div className="banner banner-error" role="alert">
            <p>{error.message}</p>
            <button type="button" className="button" onClick={() => void refresh()}>
              Retry
            </button>
          </div>
        )}
        {!loaded ? (
          <div className="placeholder">Loading sessions…</div>
        ) : all.length === 0 && !error ? (
          <EmptyState />
        ) : filtered.length === 0 ? (
          <div className="placeholder">No sessions match the current filter.</div>
        ) : (
          <>
            {active.length > 0 && (
              <section className="session-group">
                <h2 className="group-title">Active · {active.length}</h2>
                <div className="session-list">
                  {active.map((session) => (
                    <SessionCard key={session.id} session={session} now={now} />
                  ))}
                </div>
              </section>
            )}
            {finished.length > 0 && (
              <section className="session-group">
                <h2 className="group-title">Finished · {finished.length}</h2>
                <div className="session-list">
                  {finished.map((session) => (
                    <SessionCard key={session.id} session={session} now={now} />
                  ))}
                </div>
              </section>
            )}
          </>
        )}
      </main>
    </>
  );
}
