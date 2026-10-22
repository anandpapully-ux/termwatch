import { useState, type FormEvent } from "react";

import { ApiError, api } from "../services/api";
import { setToken } from "../services/auth";

export function LoginPage({ reason }: { reason?: string }) {
  const [value, setValue] = useState("");
  const [reveal, setReveal] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(reason ?? null);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    const token = value.trim();
    if (!token) return;
    setBusy(true);
    setError(null);
    try {
      await api.checkToken(token);
      setToken(token);
    } catch (caught) {
      setError(
        caught instanceof ApiError && caught.status === 401
          ? "That token was not accepted. Run `termwatch token show` on your computer and copy the token again."
          : caught instanceof Error
            ? caught.message
            : String(caught),
      );
    } finally {
      setBusy(false);
    }
  };

  return (
    <main className="login">
      <div className="login-card">
        <div className="brand brand-large">
          <span className="brand-mark" aria-hidden="true">
            &gt;_
          </span>
          TermWatch
        </div>
        <p>Enter the dashboard access token from your computer:</p>
        <pre className="code-block">termwatch token show</pre>
        <p className="muted">
          Tip: <code>termwatch token show --url &lt;dashboard URL&gt; --qr</code> prints a QR code that signs this device
          in.
        </p>
        <form onSubmit={submit} className="login-form">
          <label htmlFor="token">Access token</label>
          <div className="token-row">
            <input
              id="token"
              type={reveal ? "text" : "password"}
              value={value}
              onChange={(event) => setValue(event.target.value)}
              autoComplete="current-password"
              autoCapitalize="off"
              autoCorrect="off"
              spellCheck={false}
              required
            />
            <button type="button" className="button" onClick={() => setReveal((r) => !r)}>
              {reveal ? "Hide" : "Show"}
            </button>
          </div>
          {error && (
            <div className="banner banner-error" role="alert">
              {error}
            </div>
          )}
          <button type="submit" className="button button-primary" disabled={busy || !value.trim()}>
            {busy ? "Checking…" : "Sign in"}
          </button>
        </form>
        <p className="muted small">
          The token is stored only in this browser. Anyone with it can read your terminal output, so keep it private.
        </p>
      </div>
    </main>
  );
}
