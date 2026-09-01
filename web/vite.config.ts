import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In dev the server runs inside the hub container, watching the bind-mounted
// source, and the browser reaches it through caddy on the published port -- so
// hmr has to advertise that port rather than the one vite is listening on.
const published = Number(process.env.LOCALCODE_PORT ?? 8080);

export default defineConfig({
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    port: 5173,
    strictPort: true,
    // Filesystem events don't cross the bind mount, so watch the slow way.
    watch: { usePolling: true, interval: 300 },
    hmr: { clientPort: published },
  },
});
