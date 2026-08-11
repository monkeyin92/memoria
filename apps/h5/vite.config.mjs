import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { fileURLToPath } from "node:url";

const contractsFile = fileURLToPath(
  new URL(
    "../../packages/contracts/generated/typescript/multi_subject_contracts.ts",
    import.meta.url,
  ),
);

export default defineConfig({
  base: process.env.VITE_BASE_PATH || "/memoria-h5/",
  resolve: {
    alias: {
      // 唯一语义来源：canonical 生成契约（多主体/设备绑定/会话 epoch）。
      "@memoria/contracts": contractsFile,
    },
  },
  optimizeDeps: {
    include: ["react", "react-dom/client"],
  },
  build: {
    // 主业务 chunk 已降至 ~280kB；livekit-client 是固定供应商依赖（~509kB
    // min），缓存独立且无法再拆，阈值 550 仅消除该固定块的噪音告警。
    chunkSizeWarningLimit: 550,
    rollupOptions: {
      output: {
        // 供应商依赖独立分块：主 chunk 只保留业务代码，避免每次发布都
        // 重新下载 React/LiveKit，并消除 >500k 主包警告。
        manualChunks(id) {
          if (id.includes("node_modules/react") || id.includes("node_modules/scheduler")) {
            return "react-vendor";
          }
          if (id.includes("node_modules/livekit-client") || id.includes("node_modules/livekit")) {
            return "livekit-vendor";
          }
          if (id.includes("node_modules/@phosphor-icons")) {
            return "icons-vendor";
          }
          if (id.includes("node_modules/@fontsource")) {
            return "fonts-vendor";
          }
          return undefined;
        },
      },
    },
  },
  server: {
    host: "0.0.0.0",
    allowedHosts: ["terminal.local"],
    fs: {
      allow: ["..", "../.."],
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
