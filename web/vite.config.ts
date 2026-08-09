/// <reference types="vitest" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  build: {
    // Long-cache hashed assets; index.html is synced separately with a short TTL.
    assetsDir: "assets",
    sourcemap: true,
  },
  server: {
    host: "127.0.0.1",
    port: 8088,
    proxy: {
      // Dev only. In production the page and the API share a hostname via
      // CloudFront, so these are same-origin and no proxy exists.
      "/ask": "http://127.0.0.1:8080",
      "/config": "http://127.0.0.1:8080",
      "/healthz": "http://127.0.0.1:8080",
    },
  },
  test: {
    // Node, not jsdom: the suite covers the SSE parser and pure rail-state
    // helpers. Both use web-standard APIs (ReadableStream, TextDecoder) that
    // Node provides natively, so a DOM emulation layer buys nothing.
    environment: "node",
    include: ["src/**/*.test.ts"],
  },
});
