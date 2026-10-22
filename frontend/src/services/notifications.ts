// Browser notifications for finished sessions. These only fire while the
// dashboard is open (in a tab or an installed home-screen app); they are not
// push notifications. Browsers require a secure context (HTTPS or localhost).

const PREF_KEY = "termwatch.notifications";

export type NotificationSupport = "supported" | "insecure-context" | "unsupported";

export function notificationSupport(): NotificationSupport {
  if (!("Notification" in window)) return "unsupported";
  if (!window.isSecureContext) return "insecure-context";
  return "supported";
}

export function notificationsEnabled(): boolean {
  if (notificationSupport() !== "supported" || Notification.permission !== "granted") return false;
  try {
    return window.localStorage.getItem(PREF_KEY) !== "off";
  } catch {
    return true;
  }
}

export function setNotificationPreference(on: boolean): void {
  try {
    window.localStorage.setItem(PREF_KEY, on ? "on" : "off");
  } catch {
    // preference not persisted
  }
}

export async function enableNotifications(): Promise<NotificationPermission> {
  const permission = await Notification.requestPermission();
  setNotificationPreference(permission === "granted");
  return permission;
}

export async function registerServiceWorker(): Promise<void> {
  if (!("serviceWorker" in navigator) || !window.isSecureContext) return;
  try {
    await navigator.serviceWorker.register("/sw.js");
  } catch (error) {
    console.warn("TermWatch: service worker registration failed", error);
  }
}

export async function showNotification(title: string, body: string, url: string, tag: string): Promise<void> {
  if (!notificationsEnabled()) return;
  const options: NotificationOptions = { body, tag, icon: "/icon.svg", data: { url } };
  // Android Chrome only allows notifications via a service worker registration.
  if ("serviceWorker" in navigator) {
    const registration = await navigator.serviceWorker.getRegistration();
    if (registration) {
      await registration.showNotification(title, options);
      return;
    }
  }
  try {
    const notification = new Notification(title, options);
    notification.onclick = () => {
      window.focus();
      window.location.assign(url);
    };
  } catch (error) {
    console.warn("TermWatch: could not show notification", error);
  }
}
