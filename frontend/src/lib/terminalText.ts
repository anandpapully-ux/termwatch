// Turns raw terminal output into display lines.
//
// Output arrives in arbitrary chunks, so the assembler is stateful: a "\r\n" or
// an escape sequence split across two chunks is handled correctly. Escape
// sequences (colours, cursor movement) are stripped; "\r" not followed by "\n"
// rewinds the current line, so progress bars collapse to their latest state.

// CSI, OSC (terminated by BEL or ST) and two-byte escape sequences.
const ESCAPE_SOURCE = String.raw`\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-Z\\-_])`;
const ESCAPE_GLOBAL = new RegExp(ESCAPE_SOURCE, "g");
const ESCAPE_AT_START = new RegExp(`^${ESCAPE_SOURCE}`);
const OTHER_CONTROL = /[\x00-\x07\x0b\x0c\x0e-\x1a\x1c-\x1f\x7f]/g;
const LINE_TOKENS = /(\r\n|\n|\r|\x08)/;
const MAX_ESCAPE_CARRY = 256;

export function stripAnsi(text: string): string {
  return text.replace(ESCAPE_GLOBAL, "");
}

export class LineAssembler {
  /** Completed lines. */
  readonly lines: string[] = [];
  /** The line currently being written (no trailing newline yet). */
  current = "";
  /** Lines discarded from the front because of `maxLines`. */
  droppedLines = 0;

  private carriageReturn = false;
  private escapeCarry = "";

  constructor(private readonly maxLines = 50_000) {}

  push(text: string): void {
    let input = this.escapeCarry + text;
    this.escapeCarry = "";

    // Hold back an escape sequence that is cut off at the end of this chunk.
    const lastEscape = input.lastIndexOf("\x1b");
    if (lastEscape !== -1 && input.length - lastEscape <= MAX_ESCAPE_CARRY) {
      const tail = input.slice(lastEscape);
      if (!ESCAPE_AT_START.test(tail)) {
        this.escapeCarry = tail;
        input = input.slice(0, lastEscape);
      }
    }

    const cleaned = input.replace(ESCAPE_GLOBAL, "").replace(OTHER_CONTROL, "").replace(/\x1b/g, "");
    for (const token of cleaned.split(LINE_TOKENS)) {
      if (token === "") continue;
      if (token === "\n" || token === "\r\n") {
        this.lines.push(this.current);
        this.current = "";
        this.carriageReturn = false;
      } else if (token === "\r") {
        this.carriageReturn = true;
      } else if (token === "\x08") {
        this.current = this.current.slice(0, -1);
      } else {
        if (this.carriageReturn) {
          this.current = "";
          this.carriageReturn = false;
        }
        this.current += token;
      }
    }
    this.trim();
  }

  /** All lines including the partial current one. */
  snapshot(): string[] {
    return this.current ? [...this.lines, this.current] : [...this.lines];
  }

  get lineCount(): number {
    return this.lines.length + (this.current ? 1 : 0);
  }

  private trim(): void {
    const excess = this.lines.length - this.maxLines;
    if (excess > 0) {
      this.lines.splice(0, excess);
      this.droppedLines += excess;
    }
  }
}
