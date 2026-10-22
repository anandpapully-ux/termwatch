import { useEffect, useState } from "react";

import { useSessions } from "./useSessions";

/** Current server time in epoch seconds, ticking every `intervalMs`. */
export function useServerNow(intervalMs = 1000): number {
  const { clockOffset } = useSessions();
  const [now, setNow] = useState(() => Date.now() / 1000);
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now() / 1000), intervalMs);
    return () => window.clearInterval(timer);
  }, [intervalMs]);
  return now + clockOffset;
}
