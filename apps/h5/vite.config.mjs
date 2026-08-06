import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  base: process.env.VITE_BASE_PATH || "/memoria-h5/",
  optimizeDeps: {
    include: ["react", "react-dom/client"],
  },
  server: {
    host: "0.0.0.0",
    allowedHosts: ["terminal.local"],
    fs: {
      allow: [".."],
    },
    port: 4173,
    proxy: {
      "/memoria-api": {
        target: process.env.VITE_CONTROL_API_PROXY || "http://127.0.0.1:8000",
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/memoria-api/, ""),
        // The upstream API sees /v1/auth after rewrite. Expose the refresh
        // cookie on the browser-facing proxy path so reload keeps the session.
        cookiePathRewrite: {
          "/v1/auth": "/memoria-api/v1/auth",
        },
      },
    },
    warmup: {
      clientFiles: ["./src/main.jsx"],
    },
  },
  plugins: [react()],
  test: {
    environment: "jsdom",
    setupFiles: "./src/test/setup.js",
  },
});
