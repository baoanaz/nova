import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { site } from "./site.config";

// HTML 标题与 React 品牌读取同一配置；转义防止名称被当成 HTML。
const htmlEscapes: Record<string, string> = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };

// 开发期把 /api 与 /healthz 代理到本地 service：生产由 Caddy 同源承担（Module/07 §3），
// 因此**不要求 service 提供 CORS**（service 目前也没有 CORS 中间件）。
const target = process.env["NOVA_WEB_API"] ?? "http://127.0.0.1:8787";

// 部署（TASK-092）：SPA 挂在反向代理的子路径下（如 https://host/nova-web/）时，产物里的
// 资源 URL 与 router basename 必须同时带前缀——两者都来自这里的 `base`，缺一不可。
// 不设 `NOVA_WEB_BASE` 时仍是 `"/"`（本地开发与根路径部署行为不变）。
const base = process.env["NOVA_WEB_BASE"] ?? "/";

export default defineConfig({
  base,
  plugins: [
    react(),
    {
      name: "site-brand",
      transformIndexHtml(html) {
        const name = site.name.replace(/[&<>"']/g, (char) => htmlEscapes[char]!);
        return html.replace("__APP_NAME__", () => name);
      },
    },
  ],
  server: {
    port: 5173,
    proxy: {
      "/api": { target, changeOrigin: false },
      "/healthz": { target, changeOrigin: false },
    },
  },
  build: { outDir: "dist", sourcemap: true },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    include: ["src/**/*.test.{ts,tsx}"],
  },
});
