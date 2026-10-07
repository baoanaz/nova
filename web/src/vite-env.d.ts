/// <reference types="vitest" />

declare module "*.webp" {
  const url: string;
  export default url;
}

interface ImportMetaEnv {
  /**
   * API 基址覆盖（默认同源）。
   * 用于 web 与服务不同源部署，以及端到端测试直连真实服务。
   */
  readonly VITE_NOVA_API_BASE?: string;
  /**
   * Vite 的 `base`（子路径部署时的前缀，如 `/nova-web/`）——router basename 用它（TASK-092）。
   */
  readonly BASE_URL: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
