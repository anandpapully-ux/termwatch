import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// The TermWatch server the dev server proxies to. Override with TERMWATCH_DEV_API.
const apiTarget = process.env.TERMWATCH_DEV_API ?? "http://127.0.0.1:8765";

export default defineConfig({
  plugins: [react()],
  build: {
    // Built straight into the Python package so `termwatch server` serves it.
    outDir: "../backend/src/termwatch/web",
    emptyOutDir: true,
    sourcemap: false,
  },
  server: {
    host: "127.0.0.1",
    port: 5173,
    strictPort: true,
    proxy: {
      "/api": {
        target: apiTarget,
        changeOrigin: true,
        ws: true,
        configure: (proxy) => {
          // The server only accepts same-origin WebSockets; present the proxied
          // connection as coming from the API origin.
          proxy.on("proxyReqWs", (proxyReq) => {
            proxyReq.setHeader("origin", apiTarget);
          });
        },
      },
    },
  },
  test: {
    environment: "node",
    include: ["src/**/*.test.ts"],
  },
});
