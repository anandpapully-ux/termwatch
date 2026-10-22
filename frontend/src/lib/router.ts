// Minimal History-API router: two routes do not justify a routing library.

import { useEffect, useState } from "react";

const listeners = new Set<() => void>();

export function navigate(path: string): void {
  if (path === window.location.pathname) return;
  window.history.pushState(null, "", path);
  listeners.forEach((listener) => listener());
  window.scrollTo(0, 0);
}

export function usePath(): string {
  const [path, setPath] = useState(window.location.pathname);
  useEffect(() => {
    const update = () => setPath(window.location.pathname);
    listeners.add(update);
    window.addEventListener("popstate", update);
    return () => {
      listeners.delete(update);
      window.removeEventListener("popstate", update);
    };
  }, []);
  return path;
}

export type Route = { name: "overview" } | { name: "session"; id: string };

const SESSION_PATH = /^\/sessions\/([0-9a-f-]{36})\/?$/i;

export function matchRoute(path: string): Route {
  const match = SESSION_PATH.exec(path);
  if (match?.[1]) return { name: "session", id: match[1].toLowerCase() };
  return { name: "overview" };
}

export function sessionPath(id: string): string {
  return `/sessions/${id}`;
}
