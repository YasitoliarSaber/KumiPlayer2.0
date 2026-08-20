import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { readFileSync } from "node:fs";
import { execSync } from "node:child_process";

const packageJson = JSON.parse(
  readFileSync(new URL("./package.json", import.meta.url), "utf8"),
) as { version: string };

// Build provenance：构建期注入 Git commit / branch / 构建时间。
// 环境变量优先（build_tauri.bat 显式注入），缺失时回退到本地 git 读取；
// 两者都不可用时给出明确占位，绝不运行时调用 git 或联网。
function readGitProvenance() {
  const envSha = process.env.KUMI_BUILD_SHA || "";
  const envBranch = process.env.KUMI_BUILD_BRANCH || "";
  const envTime = process.env.KUMI_BUILD_TIME || "";
  if (envSha && envBranch) {
    return { sha: envSha, branch: envBranch, time: envTime || new Date().toISOString() };
  }
  try {
    const sha = execSync("git rev-parse HEAD", { encoding: "utf8" }).trim();
    const branch =
      process.env.KUMI_BUILD_BRANCH ||
      execSync("git branch --show-current", { encoding: "utf8" }).trim() ||
      "detached";
    return { sha, branch, time: envTime || new Date().toISOString() };
  } catch {
    return { sha: "unknown", branch: "unknown", time: envTime || new Date().toISOString() };
  }
}

const provenance = readGitProvenance();

export default defineConfig({
  plugins: [react(), tailwindcss()],
  define: {
    __APP_VERSION__: JSON.stringify(packageJson.version),
    __BUILD_SHA__: JSON.stringify(provenance.sha),
    __BUILD_BRANCH__: JSON.stringify(provenance.branch),
    __BUILD_TIME__: JSON.stringify(provenance.time),
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
