// Stores the dashboard access token. The token arrives either typed into the
// sign-in form or in the URL fragment (#token=...), which browsers never send
// to the server, and is removed from the address bar immediately.

const STORAGE_KEY = "termwatch.accessToken";

let memoryToken: string | null = null;
const listeners = new Set<() => void>();

function readStorage(): string | null {
  try {
    return window.localStorage.getItem(STORAGE_KEY);
  } catch {
    return null; // storage disabled (private mode); fall back to memory
  }
}

export function getToken(): string | null {
  return memoryToken ?? readStorage();
}

export function setToken(token: string): void {
  memoryToken = token;
  try {
    window.localStorage.setItem(STORAGE_KEY, token);
  } catch {
    // Keep the in-memory copy; the user will need to sign in again next visit.
  }
  listeners.forEach((listener) => listener());
}

export function clearToken(): void {
  memoryToken = null;
  try {
    window.localStorage.removeItem(STORAGE_KEY);
  } catch {
    // nothing stored
  }
  listeners.forEach((listener) => listener());
}

export function onTokenChange(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** Moves a `#token=...` fragment into storage and scrubs it from the URL. */
export function consumeTokenFromUrl(): boolean {
  const hash = window.location.hash;
  if (!hash.startsWith("#")) return false;
  const params = new URLSearchParams(hash.slice(1));
  const token = params.get("token");
  if (!token) return false;
  setToken(token.trim());
  const clean = window.location.pathname + window.location.search;
  window.history.replaceState(null, "", clean);
  return true;
}
