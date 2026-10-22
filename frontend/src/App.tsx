import { useEffect, useState } from "react";

import { useCompletionNotifications } from "./hooks/useCompletionNotifications";
import { useConnectionState } from "./hooks/useConnectionState";
import { SessionsProvider, useSessions } from "./hooks/useSessions";
import { matchRoute, usePath } from "./lib/router";
import { LoginPage } from "./pages/LoginPage";
import { OverviewPage } from "./pages/OverviewPage";
import { SessionPage } from "./pages/SessionPage";
import { getToken, onTokenChange } from "./services/auth";
import { liveSocket } from "./services/liveSocket";

function useToken(): string | null {
  const [token, setTokenState] = useState(getToken());
  useEffect(() => onTokenChange(() => setTokenState(getToken())), []);
  return token;
}

function Dashboard() {
  const path = usePath();
  const route = matchRoute(path);
  const { sessions, loaded } = useSessions();
  useCompletionNotifications(sessions, loaded);
  return route.name === "session" ? <SessionPage key={route.id} id={route.id} /> : <OverviewPage />;
}

export function App() {
  const token = useToken();
  const connection = useConnectionState();

  useEffect(() => {
    if (token) liveSocket.restart();
    else liveSocket.stop();
  }, [token]);

  if (!token) {
    return <LoginPage />;
  }
  if (connection === "unauthorized") {
    return <LoginPage reason="Your access token is no longer valid (it may have been rotated). Sign in again." />;
  }
  return (
    <SessionsProvider key={token}>
      <Dashboard />
    </SessionsProvider>
  );
}
