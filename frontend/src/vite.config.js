import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    host: true,
    // allow http://<workstation-name>:5173, not only localhost (Vite blocks unknown hostnames by default)
    allowedHosts: true,
    proxy: {
      "/api": process.env.BACKEND_URL || "http://localhost:8000",
    },
  },
});
