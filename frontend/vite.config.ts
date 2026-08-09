import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// The proxy is the whole reason the app can call a bare "/api/..." everywhere.
// In dev the browser talks to Vite on 41800; anything starting with /api gets
// forwarded to the API on 41700. In production nginx does the identical job.
// So no code ever needs to know an API host, and there is no VITE_API_BASE_URL
// to get wrong per environment.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 41800,
    proxy: {
      "/api": {
        target: "http://localhost:41700",
        changeOrigin: true,
      },
    },
  },
});
