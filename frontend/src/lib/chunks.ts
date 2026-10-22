import type { LogChunk } from "../types/api";

export interface FreshChunks {
  /** Chunks newer than `lastSeq`, ascending, without duplicates. */
  fresh: LogChunk[];
  /** Highest sequence number seen after applying `fresh`. */
  lastSeq: number;
  /** True if sequence numbers skipped ahead (output missing in between). */
  gap: boolean;
}

/**
 * Filters an incoming batch against what the viewer already has. Sequence
 * numbers are assigned by the CLI and are unique per session, so anything at
 * or below `lastSeq` is a duplicate (e.g. replayed after a reconnect).
 */
export function selectFreshChunks(lastSeq: number, chunks: readonly LogChunk[]): FreshChunks {
  const sorted = [...chunks].sort((a, b) => a.seq - b.seq);
  const fresh: LogChunk[] = [];
  let seq = lastSeq;
  let gap = false;
  for (const chunk of sorted) {
    if (chunk.seq <= seq) continue;
    if (seq >= 0 && chunk.seq !== seq + 1) gap = true;
    fresh.push(chunk);
    seq = chunk.seq;
  }
  return { fresh, lastSeq: seq, gap };
}
