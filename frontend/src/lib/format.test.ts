import { describe, expect, it } from "vitest";

import { formatBytes, formatDuration } from "./format";

describe("formatDuration", () => {
  it("formats seconds, minutes and hours", () => {
    expect(formatDuration(5.9)).toBe("5s");
    expect(formatDuration(512)).toBe("8m 32s");
    expect(formatDuration(3725)).toBe("1h 02m 05s");
    expect(formatDuration(-3)).toBe("0s");
  });
});

describe("formatBytes", () => {
  it("uses binary units", () => {
    expect(formatBytes(512)).toBe("512 B");
    expect(formatBytes(2048)).toBe("2.0 KiB");
    expect(formatBytes(5 * 1024 * 1024)).toBe("5.0 MiB");
  });
});
