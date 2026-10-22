import { useCallback, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";

import { copyText } from "../lib/clipboard";

// Rendering tens of thousands of DOM rows is slow on phones; show the tail and
// let the user expand on demand. Search always scans every loaded line.
const RENDER_STEP = 2000;
const BOTTOM_THRESHOLD_PX = 32;

interface LogViewerProps {
  lines: string[];
  loading: boolean;
  hasEarlier: boolean;
  loadingEarlier: boolean;
  onLoadEarlier: () => void;
  notice?: ReactNode;
}

function escapeRegExp(text: string): string {
  return text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function highlight(line: string, matcher: RegExp | null): ReactNode {
  if (!matcher) return line || " ";
  const parts = line.split(matcher);
  return parts.map((part, index) =>
    index % 2 === 1 ? (
      <mark key={index} className="log-match">
        {part}
      </mark>
    ) : (
      part
    ),
  );
}

export function LogViewer({ lines, loading, hasEarlier, loadingEarlier, onLoadEarlier, notice }: LogViewerProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [follow, setFollow] = useState(true);
  const [query, setQuery] = useState("");
  const [renderLimit, setRenderLimit] = useState(RENDER_STEP);
  const [copyState, setCopyState] = useState<"idle" | "copied" | "failed">("idle");

  const trimmedQuery = query.trim();
  const matcher = useMemo(
    () => (trimmedQuery ? new RegExp(`(${escapeRegExp(trimmedQuery)})`, "gi") : null),
    [trimmedQuery],
  );

  const matches = useMemo(() => {
    if (!trimmedQuery) return null;
    const needle = trimmedQuery.toLowerCase();
    return lines.filter((line) => line.toLowerCase().includes(needle));
  }, [lines, trimmedQuery]);

  const source = matches ?? lines;
  const hiddenLocally = Math.max(0, source.length - renderLimit);
  const visible = hiddenLocally > 0 ? source.slice(-renderLimit) : source;

  useLayoutEffect(() => {
    const element = containerRef.current;
    if (element && follow) element.scrollTop = element.scrollHeight;
  }, [visible, follow]);

  const onScroll = useCallback(() => {
    const element = containerRef.current;
    if (!element) return;
    const atBottom = element.scrollHeight - element.scrollTop - element.clientHeight < BOTTOM_THRESHOLD_PX;
    setFollow(atBottom);
  }, []);

  const jumpToLatest = () => {
    setFollow(true);
    const element = containerRef.current;
    if (element) element.scrollTop = element.scrollHeight;
  };

  const copyVisible = async () => {
    const ok = await copyText(visible.join("\n"));
    setCopyState(ok ? "copied" : "failed");
    window.setTimeout(() => setCopyState("idle"), 2000);
  };

  return (
    <section className="log-panel" aria-label="Process output">
      <div className="log-toolbar">
        <input
          className="log-search"
          type="search"
          placeholder="Search output"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          aria-label="Search output"
          autoCapitalize="off"
          autoCorrect="off"
          spellCheck={false}
        />
        <button
          type="button"
          className="button"
          onClick={() => (follow ? setFollow(false) : jumpToLatest())}
          aria-pressed={!follow}
        >
          {follow ? "Pause scroll" : "Follow"}
        </button>
        <button type="button" className="button" onClick={copyVisible} disabled={visible.length === 0}>
          {copyState === "copied" ? "Copied" : copyState === "failed" ? "Copy failed" : "Copy"}
        </button>
      </div>
      {matches && (
        <div className="log-search-summary" role="status">
          {matches.length === 0
            ? "No matching lines"
            : `${matches.length} matching line${matches.length === 1 ? "" : "s"} (of ${lines.length} loaded)`}
          {hasEarlier && " · older output not loaded is not searched"}
        </div>
      )}
      {notice}
      <div className="log-output" ref={containerRef} onScroll={onScroll} tabIndex={0}>
        {(hiddenLocally > 0 || (hasEarlier && !matches)) && (
          <div className="log-earlier">
            {hiddenLocally > 0 ? (
              <button type="button" className="button button-small" onClick={() => setRenderLimit((n) => n + RENDER_STEP)}>
                Show {Math.min(RENDER_STEP, hiddenLocally)} earlier lines ({hiddenLocally} hidden)
              </button>
            ) : (
              <button type="button" className="button button-small" onClick={onLoadEarlier} disabled={loadingEarlier}>
                {loadingEarlier ? "Loading…" : "Load earlier output"}
              </button>
            )}
          </div>
        )}
        {loading && lines.length === 0 ? (
          <div className="log-placeholder">Loading output…</div>
        ) : visible.length === 0 ? (
          <div className="log-placeholder">{matches ? "No matches." : "No output yet."}</div>
        ) : (
          visible.map((line, index) => (
            <div className="log-line" key={source.length - visible.length + index}>
              {highlight(line, matcher)}
            </div>
          ))
        )}
      </div>
      {!follow && (
        <button type="button" className="jump-latest" onClick={jumpToLatest}>
          ↓ Latest
        </button>
      )}
    </section>
  );
}
