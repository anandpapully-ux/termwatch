import { describe, expect, it } from "vitest";

import type { LogChunk } from "../types/api";
import { selectFreshChunks } from "./chunks";

const chunk = (seq: number): LogChunk => ({ seq, ts: 1, data: `line ${seq}\n` });

describe("selectFreshChunks", () => {
  it("accepts everything newer than lastSeq in order", () => {
    const result = selectFreshChunks(-1, [chunk(0), chunk(1), chunk(2)]);
    expect(result.fresh.map((c) => c.seq)).toEqual([0, 1, 2]);
    expect(result.lastSeq).toBe(2);
    expect(result.gap).toBe(false);
  });

  it("drops chunks replayed after a reconnect", () => {
    const result = selectFreshChunks(4, [chunk(3), chunk(4), chunk(5), chunk(5), chunk(6)]);
    expect(result.fresh.map((c) => c.seq)).toEqual([5, 6]);
    expect(result.lastSeq).toBe(6);
  });

  it("sorts out-of-order input", () => {
    const result = selectFreshChunks(0, [chunk(3), chunk(1), chunk(2)]);
    expect(result.fresh.map((c) => c.seq)).toEqual([1, 2, 3]);
  });

  it("reports gaps", () => {
    expect(selectFreshChunks(1, [chunk(5)]).gap).toBe(true);
  });

  it("does not treat a trimmed start as a gap", () => {
    expect(selectFreshChunks(-1, [chunk(40)]).gap).toBe(false);
  });
});
