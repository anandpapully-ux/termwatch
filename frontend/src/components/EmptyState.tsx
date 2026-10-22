export function EmptyState() {
  return (
    <div className="empty-state">
      <h2>No sessions yet</h2>
      <p>Start a command through TermWatch on your computer and it will appear here instantly.</p>
      <pre className="code-block">termwatch run --name "PDF Parser" -- python parser.py</pre>
      <p className="muted">
        Everything after <code>--</code> is the command to run. Its output still appears in your terminal, and the exit
        code is passed through unchanged.
      </p>
    </div>
  );
}
