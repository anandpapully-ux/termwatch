import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { App } from "./App";
import { consumeTokenFromUrl } from "./services/auth";
import { registerServiceWorker } from "./services/notifications";
import "./styles.css";

consumeTokenFromUrl();
// Opening a sign-in link in a tab already showing the dashboard only changes the hash.
window.addEventListener("hashchange", () => consumeTokenFromUrl());
void registerServiceWorker();

const root = document.getElementById("root");
if (!root) throw new Error("missing #root element");
createRoot(root).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
