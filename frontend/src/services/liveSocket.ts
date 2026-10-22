// WebSocket client for live updates, with automatic reconnection.
//
// After every (re)connection the server sends "hello". Consumers use that as the
// signal to re-fetch anything they may have missed, and every active log
// subscription is re-sent with the last sequence number already received, so
// the server backfills exactly the missing chunks.

import type { ServerMessage } from "../types/api";
import { getToken } from "./auth";

export type ConnectionState = "idle" | "connecting" | "open" | "reconnecting" | "unauthorized";

type MessageListener = (message: ServerMessage) => void;
type StateListener = (state: ConnectionState) => void;

const CLOSE_UNAUTHORIZED = 4401;
const MAX_BACKOFF_MS = 15_000;
// The server pings every 15 s; silence for longer than this means a dead link
// (common on phones switching networks or waking from sleep).
const STALE_AFTER_MS = 40_000;

export class LiveSocket {
  private ws: WebSocket | null = null;
  private state: ConnectionState = "idle";
  private attempts = 0;
  private retryTimer: number | null = null;
  private watchdogTimer: number | null = null;
  private lastMessageAt = 0;
  private running = false;
  private readonly messageListeners = new Set<MessageListener>();
  private readonly stateListeners = new Set<StateListener>();
  private readonly subscriptions = new Map<string, () => number>();

  start(): void {
    if (this.running) return;
    this.running = true;
    window.addEventListener("online", this.reconnectNow);
    document.addEventListener("visibilitychange", this.onVisibility);
    this.watchdogTimer = window.setInterval(this.checkStale, 5_000);
    this.connect();
  }

  stop(): void {
    this.running = false;
    window.removeEventListener("online", this.reconnectNow);
    document.removeEventListener("visibilitychange", this.onVisibility);
    if (this.watchdogTimer !== null) window.clearInterval(this.watchdogTimer);
    if (this.retryTimer !== null) window.clearTimeout(this.retryTimer);
    this.watchdogTimer = this.retryTimer = null;
    this.teardown();
    this.setState("idle");
  }

  getState(): ConnectionState {
    return this.state;
  }

  onMessage(listener: MessageListener): () => void {
    this.messageListeners.add(listener);
    return () => this.messageListeners.delete(listener);
  }

  onState(listener: StateListener): () => void {
    this.stateListeners.add(listener);
    return () => this.stateListeners.delete(listener);
  }

  /**
   * Receive chunks for a session. `afterSeq` is read at (re)subscribe time so the
   * server only sends what this client has not seen yet.
   */
  subscribe(sessionId: string, afterSeq: () => number): () => void {
    this.subscriptions.set(sessionId, afterSeq);
    this.sendSubscribe(sessionId, afterSeq());
    return () => {
      if (this.subscriptions.get(sessionId) === afterSeq) {
        this.subscriptions.delete(sessionId);
        this.send({ type: "unsubscribe", session_id: sessionId });
      }
    };
  }

  private sendSubscribe(sessionId: string, afterSeq: number): void {
    if (this.state === "open") {
      this.send({ type: "subscribe", session_id: sessionId, after_seq: afterSeq });
    }
  }

  private send(message: object): void {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(message));
    }
  }

  private setState(state: ConnectionState): void {
    if (state === this.state) return;
    this.state = state;
    this.stateListeners.forEach((listener) => listener(state));
  }

  private connect(): void {
    const token = getToken();
    if (!token) {
      this.setState("unauthorized");
      return;
    }
    this.teardown();
    this.setState(this.attempts === 0 ? "connecting" : "reconnecting");
    const scheme = window.location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${scheme}://${window.location.host}/api/ws`);
    this.ws = ws;
    ws.onopen = () => {
      this.lastMessageAt = Date.now();
      ws.send(JSON.stringify({ type: "auth", token }));
    };
    ws.onmessage = (event: MessageEvent<string>) => {
      this.lastMessageAt = Date.now();
      let message: ServerMessage;
      try {
        message = JSON.parse(event.data) as ServerMessage;
      } catch {
        console.warn("TermWatch: ignoring malformed WebSocket frame");
        return;
      }
      if (message.type === "hello") {
        this.attempts = 0;
        this.setState("open");
        this.subscriptions.forEach((afterSeq, sessionId) => this.sendSubscribe(sessionId, afterSeq()));
      }
      this.messageListeners.forEach((listener) => listener(message));
    };
    ws.onclose = (event: CloseEvent) => {
      if (this.ws !== ws) return; // superseded by a newer connection
      this.ws = null;
      if (event.code === CLOSE_UNAUTHORIZED) {
        this.setState("unauthorized");
        return;
      }
      this.scheduleReconnect();
    };
    ws.onerror = () => {
      // onclose follows and handles reconnection.
    };
  }

  private teardown(): void {
    const ws = this.ws;
    this.ws = null;
    if (ws) {
      ws.onopen = ws.onmessage = ws.onclose = ws.onerror = null;
      try {
        ws.close();
      } catch {
        // already closed
      }
    }
  }

  private scheduleReconnect(): void {
    if (!this.running) return;
    this.setState("reconnecting");
    const base = Math.min(MAX_BACKOFF_MS, 500 * 2 ** this.attempts);
    const delay = base / 2 + Math.random() * (base / 2);
    this.attempts += 1;
    if (this.retryTimer !== null) window.clearTimeout(this.retryTimer);
    this.retryTimer = window.setTimeout(() => {
      this.retryTimer = null;
      this.connect();
    }, delay);
  }

  /** Reconnect immediately (network came back, tab became visible, token changed). */
  readonly reconnectNow = (): void => {
    if (!this.running) return;
    if (this.state === "open" && Date.now() - this.lastMessageAt < STALE_AFTER_MS) return;
    if (this.retryTimer !== null) window.clearTimeout(this.retryTimer);
    this.retryTimer = null;
    this.attempts = Math.min(this.attempts, 1);
    this.connect();
  };

  /** Restart after the user signs in with a new token. */
  restart(): void {
    if (!this.running) {
      this.start();
      return;
    }
    this.attempts = 0;
    this.connect();
  }

  private readonly onVisibility = (): void => {
    if (document.visibilityState === "visible") {
      if (this.state === "open") this.send({ type: "ping" });
      else this.reconnectNow();
    }
  };

  private readonly checkStale = (): void => {
    if (this.state === "open" && Date.now() - this.lastMessageAt > STALE_AFTER_MS) {
      console.warn("TermWatch: live connection went silent; reconnecting");
      this.teardown();
      this.scheduleReconnect();
    }
  };
}

export const liveSocket = new LiveSocket();
