import { useConnectionState } from "../hooks/useConnectionState";

export function ConnectionBanner() {
  const state = useConnectionState();
  if (state === "reconnecting") {
    return (
      <div className="banner banner-warning" role="status">
        Live connection lost — reconnecting. Updates will resume and missed output will be fetched automatically.
      </div>
    );
  }
  return null;
}

export function ConnectionDot() {
  const state = useConnectionState();
  const label =
    state === "open" ? "Live" : state === "connecting" ? "Connecting" : state === "reconnecting" ? "Offline" : "Idle";
  return (
    <span className={`connection-dot connection-${state}`} title={`Live updates: ${label}`}>
      <span aria-hidden="true" className="dot" />
      {label}
    </span>
  );
}
