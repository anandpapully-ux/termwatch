// Typed REST client. All backend HTTP traffic goes through here.

import type { AuthCheck, LogPage, Session, SessionList } from "../types/api";
import { clearToken, getToken } from "./auth";

export class ApiError extends Error {
  /** HTTP status, or 0 when the server could not be reached at all. */
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

async function request<T>(path: string, token: string | null = getToken()): Promise<T> {
  let response: Response;
  try {
    response = await fetch(path, {
      headers: token ? { Authorization: `Bearer ${token}` } : {},
      cache: "no-store",
    });
  } catch {
    throw new ApiError(
      0,
      "Can't reach the TermWatch server. Check that `termwatch server` is running and that this device is on the same network or VPN.",
    );
  }
  if (response.status === 401) {
    clearToken();
    throw new ApiError(401, "The access token was rejected. Sign in again with `termwatch token show`.");
  }
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = (await response.json()) as { detail?: unknown };
      if (typeof body.detail === "string") detail = body.detail;
    } catch {
      // non-JSON error body; keep the status text
    }
    throw new ApiError(response.status, detail || `Request failed with HTTP ${response.status}`);
  }
  return (await response.json()) as T;
}

export interface LogQuery {
  afterSeq?: number;
  beforeSeq?: number;
  limit?: number;
}

export const api = {
  checkToken(token: string): Promise<AuthCheck> {
    return request<AuthCheck>("/api/auth/check", token);
  },

  listSessions(limit = 1000): Promise<SessionList> {
    return request<SessionList>(`/api/sessions?limit=${limit}`);
  },

  getSession(id: string): Promise<Session> {
    return request<Session>(`/api/sessions/${encodeURIComponent(id)}`);
  },

  getLogs(id: string, query: LogQuery = {}): Promise<LogPage> {
    const params = new URLSearchParams();
    if (query.afterSeq !== undefined) params.set("after_seq", String(query.afterSeq));
    if (query.beforeSeq !== undefined) params.set("before_seq", String(query.beforeSeq));
    if (query.limit !== undefined) params.set("limit", String(query.limit));
    const suffix = params.toString() ? `?${params}` : "";
    return request<LogPage>(`/api/sessions/${encodeURIComponent(id)}/logs${suffix}`);
  },
};
