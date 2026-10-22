import { useState, type ReactNode } from "react";

import { navigate } from "../lib/router";
import { clearToken } from "../services/auth";
import {
  enableNotifications,
  notificationSupport,
  notificationsEnabled,
  setNotificationPreference,
} from "../services/notifications";
import { ConnectionDot } from "./ConnectionBanner";

function NotificationButton() {
  const support = notificationSupport();
  const [enabled, setEnabled] = useState(notificationsEnabled());
  const [message, setMessage] = useState<string | null>(null);

  if (support === "unsupported") return null;

  const toggle = async () => {
    setMessage(null);
    if (support === "insecure-context") {
      setMessage(
        "Browsers only allow notifications over HTTPS or on localhost. Use `tailscale serve` (see docs) to get HTTPS.",
      );
      return;
    }
    if (enabled) {
      setNotificationPreference(false);
      setEnabled(false);
      return;
    }
    const permission = await enableNotifications();
    setEnabled(permission === "granted");
    if (permission === "denied") setMessage("Notifications are blocked for this site in your browser settings.");
  };

  return (
    <>
      <button type="button" className="button button-ghost" onClick={toggle} aria-pressed={enabled}>
        {enabled ? "Alerts on" : "Alerts off"}
      </button>
      {message && (
        <div className="header-message" role="status">
          {message}
          <button type="button" className="link-button" onClick={() => setMessage(null)}>
            Dismiss
          </button>
        </div>
      )}
    </>
  );
}

export function AppHeader({ back, children }: { back?: boolean; children?: ReactNode }) {
  const signOut = () => {
    if (window.confirm("Sign out of TermWatch on this device?")) clearToken();
  };
  return (
    <header className="app-header">
      <div className="app-header-row">
        {back ? (
          <button type="button" className="button button-ghost back-button" onClick={() => navigate("/")}>
            ← Sessions
          </button>
        ) : (
          <a
            className="brand"
            href="/"
            onClick={(event) => {
              event.preventDefault();
              navigate("/");
            }}
          >
            <span className="brand-mark" aria-hidden="true">
              &gt;_
            </span>
            TermWatch
          </a>
        )}
        <div className="header-actions">
          <ConnectionDot />
          <NotificationButton />
          <button type="button" className="button button-ghost" onClick={signOut} title="Sign out">
            Sign out
          </button>
        </div>
      </div>
      {children}
    </header>
  );
}
