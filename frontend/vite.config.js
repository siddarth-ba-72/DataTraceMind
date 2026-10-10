import { resolve } from "node:path";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Two pages are built: the incident console at / and the Scenario Lab at /lab/.
// In development they run on :5173 and forward API calls to the FastAPI
// service. In production FastAPI serves the built files itself.
export default defineConfig({
  plugins: [react()],
  build: {
    rollupOptions: {
      input: {
        console: resolve(import.meta.dirname, "index.html"),
        lab: resolve(import.meta.dirname, "lab/index.html"),
      },
    },
  },
  server: {
    port: 5173,
    proxy: { "/api": "http://127.0.0.1:8000" },
  },
});
