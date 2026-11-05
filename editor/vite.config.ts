import { fileURLToPath } from "node:url";
import vue from "@vitejs/plugin-vue";
import { defineConfig } from "vite";

// The runtime serves the build under <root>/editor/, where <root> is "" or the Home Assistant
// Ingress prefix, so built asset URLs are relative. `npm run dev` proxies the API to a runtime.
const runtime = process.env.FLOWPILOT_URL ?? "http://127.0.0.1:8765";

export default defineConfig({
  base: "./",
  plugins: [vue()],
  build: {
    outDir: fileURLToPath(new URL("../packages/flowpilot-server/src/flowpilot_server/editor", import.meta.url)),
    emptyOutDir: true,
  },
  server: {
    proxy: { "/api": { target: runtime, ws: true } },
  },
});
