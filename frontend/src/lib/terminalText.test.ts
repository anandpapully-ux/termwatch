import { describe, expect, it } from "vitest";

import { LineAssembler, stripAnsi } from "./terminalText";

function assemble(...chunks: string[]): string[] {
  const assembler = new LineAssembler();
  chunks.forEach((chunk) => assembler.push(chunk));
  return assembler.snapshot();
}

describe("LineAssembler", () => {
  it("splits lines on LF and CRLF", () => {
    expect(assemble("one\r\ntwo\nthree")).toEqual(["one", "two", "three"]);
  });

  it("handles CRLF split across chunks without losing the line", () => {
    expect(assemble("Processed page 1\r", "\nProcessed page 2\r\n")).toEqual([
      "Processed page 1",
      "Processed page 2",
    ]);
  });

  it("collapses carriage-return progress updates to the latest state", () => {
    expect(assemble("10%\r", "50%\r", "100%\r\ndone\n")).toEqual(["100%", "done"]);
  });

  it("strips colour codes, including ones split across chunks", () => {
    expect(assemble("\x1b[3", "2mgreen\x1b[0m text\n")).toEqual(["green text"]);
  });

  it("applies backspaces", () => {
    expect(assemble("abc\x08\x08X\n")).toEqual(["aX"]);
  });

  it("keeps empty lines", () => {
    expect(assemble("a\n\nb\n")).toEqual(["a", "", "b"]);
  });

  it("caps the number of lines kept", () => {
    const assembler = new LineAssembler(3);
    assembler.push("1\n2\n3\n4\n5\n");
    expect(assembler.snapshot()).toEqual(["3", "4", "5"]);
    expect(assembler.droppedLines).toBe(2);
  });
});

describe("helpers", () => {
  it("strips OSC title sequences", () => {
    expect(stripAnsi("\x1b]0;title\x07hello")).toBe("hello");
  });
});
