import { useEffect, useState } from "react";

import { liveSocket, type ConnectionState } from "../services/liveSocket";

export function useConnectionState(): ConnectionState {
  const [state, setState] = useState<ConnectionState>(liveSocket.getState());
  useEffect(() => {
    setState(liveSocket.getState());
    return liveSocket.onState(setState);
  }, []);
  return state;
}
