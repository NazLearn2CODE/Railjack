import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// Dev: Vite on :5173 proxies API to the Railjack hub on :8700.
// Prod: `vite build` -> dist/, served by FastAPI's StaticFiles mount.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    proxy: {
      "/api": "http://127.0.0.1:8700",
    },
  },
  build: {
    outDir: "dist", emptyOutDir: true, sourcemap: false,
    // localhost cockpit — one 134 kB-gzip bundle is fine; splitting is pointless here.
    // ponytail: if the dashboard ever gets served over the network to weak clients,
    // lazy() the two heavy panels (Newsroom ~4.5k lines, Calendar) instead of raising this.
    chunkSizeWarningLimit: 600,
  },
});
