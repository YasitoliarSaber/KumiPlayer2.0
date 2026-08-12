import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { readFileSync } from "node:fs";

const packageJson = JSON.parse(
  readFileSync(new URL("./package.json", import.meta.url), "utf8"),
) as { version: string };

export default defineConfig({
  plugins: [react(), tailwindcss()],
  define: {
    __APP_VERSION__: JSON.stringify(packageJson.version),
  },
  // 仅用于 `npm run tauri dev`：本机 Vite 开发服务。/api 与 /ws 透明代理到桌面后端 37821，
  // 生产构建仍由 frontendDist (../dist) 提供，不开放浏览器 preview 运行模式。
  server: {
    host: "127.0.0.1",
    port: 1420,
    strictPort: true,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:37821",
        changeOrigin: true,
      },
      "/ws": {
        target: "ws://127.0.0.1:37821",
        ws: true,
        changeOrigin: true,
      },
    },
  },
  clearScreen: false,
});
