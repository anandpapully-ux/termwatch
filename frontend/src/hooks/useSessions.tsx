// Shared session list. Loaded over REST, kept current by WebSocket "session"
// events, and re-fetched after every reconnect so missed updates are recovered.

import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";

import { ApiError, api } from "../services/api";
import { liveSocket } from "../services/liveSocket";
import type { Session } from "../types/api";

interface SessionsState {
  sessions: Map<string, Session>;
  loaded: boolean;
  error: ApiError | null;
  /** serverTime - localTime in seconds; phones' clocks are not always right. */
  clockOffset: number;
  refresh: () => Promise<void>;
  upsert: (session: Session) => void;
}

const SessionsContext = createContext<SessionsState | null>(null);

export function SessionsProvider({ children }: { children: ReactNode }) {
  const [sessions, setSessions] = useState<Map<string, Session>>(() => new Map());
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);
  const [clockOffset, setClockOffset] = useState(0);
  const inFlight = useRef<Promise<void> | null>(null);

  const upsert = useCallback((session: Session) => {
    setSessions((previous) => {
      const existing = previous.get(session.id);
      if (existing && existing.revision > session.revision) return previous; // stale snapshot
      const next = new Map(previous);
      next.set(session.id, session);
      return next;
    });
  }, []);

  const refresh = useCallback(async () => {
    if (inFlight.current) return inFlight.current;
    const task = (async () => {
      try {
        const list = await api.listSessions();
        setClockOffset(list.server_time - Date.now() / 1000);
        setSessions((previous) => {
          // A push that arrived while this request was in flight may be newer.
          const next = new Map<string, Session>();
          for (const session of list.sessions) {
            const existing = previous.get(session.id);
            next.set(session.id, existing && existing.revision > session.revision ? existing : session);
          }
          return next;
        });
        setError(null);
      } catch (caught) {
        setError(caught instanceof ApiError ? caught : new ApiError(0, String(caught)));
      } finally {
        setLoaded(true);
        inFlight.current = null;
      }
    })();
    inFlight.current = task;
    return task;
  }, []);

  useEffect(() => {
    void refresh();
    return liveSocket.onMessage((message) => {
      switch (message.type) {
        case "hello":
          setClockOffset(message.server_time - Date.now() / 1000);
          void refresh();
          break;
        case "ping":
          setClockOffset(message.server_time - Date.now() / 1000);
          break;
        case "session":
          upsert(message.session);
          break;
        case "sessions_deleted":
          setSessions((previous) => {
            const next = new Map(previous);
            message.session_ids.forEach((id) => next.delete(id));
            return next;
          });
          break;
        default:
          break;
      }
    });
  }, [refresh, upsert]);

  const value = useMemo(
    () => ({ sessions, loaded, error, clockOffset, refresh, upsert }),
    [sessions, loaded, error, clockOffset, refresh, upsert],
  );
  return <SessionsContext.Provider value={value}>{children}</SessionsContext.Provider>;
}

export function useSessions(): SessionsState {
  const value = useContext(SessionsContext);
  if (!value) throw new Error("useSessions must be used inside <SessionsProvider>");
  return value;
}
