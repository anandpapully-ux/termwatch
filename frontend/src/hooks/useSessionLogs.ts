// Live output for one session.
//
// 1. Load the most recent output over REST.
// 2. Subscribe over the WebSocket with the last seq received; the server
//    backfills anything newer and then streams live chunks.
// 3. On reconnect, LiveSocket re-subscribes with the current last seq, so
//    missed output is recovered. `selectFreshChunks` drops any duplicates.

import { useCallback, useEffect, useRef, useState } from "react";

import { selectFreshChunks } from "../lib/chunks";
import { LineAssembler } from "../lib/terminalText";
import { ApiError, api } from "../services/api";
import { liveSocket } from "../services/liveSocket";
import type { LogChunk } from "../types/api";

const PAGE_CHUNKS = 1000;
// Keep at most this much raw text in memory; older output can be re-fetched.
const MAX_BUFFER_CHARS = 4 * 1024 * 1024;
const MAX_LINES = 50_000;

export interface SessionLogs {
  /** Display lines (including the partial last line). */
  lines: string[];
  loading: boolean;
  error: ApiError | null;
  /** Older output exists on the server (not loaded, or trimmed from memory). */
  hasEarlier: boolean;
  loadingEarlier: boolean;
  loadEarlier: () => Promise<void>;
  /** Output before this seq was deleted by the server's retention limit. */
  firstRetainedSeq: number;
  /** A gap in sequence numbers was observed while streaming. */
  sawGap: boolean;
}

export function useSessionLogs(sessionId: string): SessionLogs {
  const chunksRef = useRef<LogChunk[]>([]);
  const bufferedCharsRef = useRef(0);
  const assemblerRef = useRef(new LineAssembler(MAX_LINES));
  const lastSeqRef = useRef(-1);
  const frameRef = useRef<number | null>(null);

  const [lines, setLines] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<ApiError | null>(null);
  const [hasEarlier, setHasEarlier] = useState(false);
  const [loadingEarlier, setLoadingEarlier] = useState(false);
  const [firstRetainedSeq, setFirstRetainedSeq] = useState(0);
  const [sawGap, setSawGap] = useState(false);

  // Coalesce bursts of chunks into one render per animation frame.
  const scheduleRender = useCallback(() => {
    if (frameRef.current !== null) return;
    frameRef.current = window.requestAnimationFrame(() => {
      frameRef.current = null;
      setLines(assemblerRef.current.snapshot());
    });
  }, []);

  const rebuild = useCallback(() => {
    const assembler = new LineAssembler(MAX_LINES);
    for (const chunk of chunksRef.current) assembler.push(chunk.data);
    assemblerRef.current = assembler;
    scheduleRender();
  }, [scheduleRender]);

  const appendChunks = useCallback(
    (incoming: readonly LogChunk[]) => {
      const { fresh, lastSeq, gap } = selectFreshChunks(lastSeqRef.current, incoming);
      if (fresh.length === 0) return;
      if (gap) setSawGap(true);
      lastSeqRef.current = lastSeq;
      for (const chunk of fresh) {
        chunksRef.current.push(chunk);
        bufferedCharsRef.current += chunk.data.length;
        assemblerRef.current.push(chunk.data);
      }
      if (bufferedCharsRef.current > MAX_BUFFER_CHARS) {
        // Drop the oldest quarter from memory; it is still on the server.
        let dropped = 0;
        while (chunksRef.current.length > 1 && bufferedCharsRef.current - dropped > MAX_BUFFER_CHARS * 0.75) {
          dropped += chunksRef.current.shift()?.data.length ?? 0;
        }
        bufferedCharsRef.current -= dropped;
        setHasEarlier(true);
        rebuild();
        return;
      }
      scheduleRender();
    },
    [rebuild, scheduleRender],
  );

  useEffect(() => {
    let cancelled = false;
    let unsubscribe: (() => void) | null = null;
    chunksRef.current = [];
    bufferedCharsRef.current = 0;
    assemblerRef.current = new LineAssembler(MAX_LINES);
    lastSeqRef.current = -1;
    setLines([]);
    setLoading(true);
    setError(null);
    setHasEarlier(false);
    setSawGap(false);

    const stopListening = liveSocket.onMessage((message) => {
      if (message.type === "chunks" && message.session_id === sessionId) {
        if (message.first_retained_seq !== undefined) setFirstRetainedSeq(message.first_retained_seq);
        appendChunks(message.chunks);
      }
    });

    api
      .getLogs(sessionId, { limit: PAGE_CHUNKS })
      .then((page) => {
        if (cancelled) return;
        setFirstRetainedSeq(page.first_retained_seq);
        setHasEarlier(page.has_more_before);
        appendChunks(page.chunks);
        if (page.chunks.length === 0) lastSeqRef.current = Math.max(lastSeqRef.current, page.first_retained_seq - 1);
        unsubscribe = liveSocket.subscribe(sessionId, () => lastSeqRef.current);
      })
      .catch((caught: unknown) => {
        if (cancelled) return;
        setError(caught instanceof ApiError ? caught : new ApiError(0, String(caught)));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    return () => {
      cancelled = true;
      stopListening();
      unsubscribe?.();
      if (frameRef.current !== null) window.cancelAnimationFrame(frameRef.current);
      frameRef.current = null;
    };
  }, [sessionId, appendChunks]);

  const loadEarlier = useCallback(async () => {
    const first = chunksRef.current[0];
    if (!first) return;
    setLoadingEarlier(true);
    try {
      const page = await api.getLogs(sessionId, { beforeSeq: first.seq, limit: PAGE_CHUNKS });
      const known = new Set(chunksRef.current.map((chunk) => chunk.seq));
      const older = page.chunks.filter((chunk) => !known.has(chunk.seq));
      chunksRef.current = [...older, ...chunksRef.current];
      bufferedCharsRef.current += older.reduce((sum, chunk) => sum + chunk.data.length, 0);
      setHasEarlier(page.has_more_before);
      setFirstRetainedSeq(page.first_retained_seq);
      rebuild();
    } catch (caught) {
      setError(caught instanceof ApiError ? caught : new ApiError(0, String(caught)));
    } finally {
      setLoadingEarlier(false);
    }
  }, [sessionId, rebuild]);

  return { lines, loading, error, hasEarlier, loadingEarlier, loadEarlier, firstRetainedSeq, sawGap };
}
