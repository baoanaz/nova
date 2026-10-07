/**
 * 端到端测试：**真实 service**（TASK-070 §DoD 的第 3 条）。
 *
 * 默认跳过——它需要一个已索引的 nova-service。启用方式：
 *
 * ```bash
 * # 终端 A
 * export NO_PROXY=127.0.0.1,localhost
 * uv run nova-service local --repo <仓库> --data-root /tmp/nova-web-check/data --port 8787
 * # 终端 B（等索引完成）
 * cd web && NOVA_E2E=1 NOVA_E2E_BASE=http://127.0.0.1:8787 npx vitest run src/pages/e2e.test.tsx
 * ```
 *
 * 为什么值得单独一个文件：单元测试用的是 mock，只能证明"我按我以为的契约解析了"。
 * 这个文件证明的是"服务端实际返回的形状与类型声明一致"——契约对齐的唯一硬证据。
 *
 * 注：TASK-082 删除了 Playground 页面，随之删掉"检索结果在 Playground 渲染"的用例；
 * 这里保留的 healthz/projects 形状校验价值独立于任何页面。
 */

import { describe, expect, it } from "vitest";

const enabled = process.env["NOVA_E2E"] === "1";
const base = process.env["NOVA_E2E_BASE"] ?? "http://127.0.0.1:8787";

describe.skipIf(!enabled)("端到端（真实 service）", () => {
  it("真实服务可达（healthz 与 projects 的形状与类型声明一致）", async () => {
    const health = (await (await fetch(`${base}/healthz`)).json()) as Record<string, unknown>;
    expect(health["status"]).toBe("ok");
    expect(typeof health["localMode"]).toBe("boolean");
    expect(Array.isArray(health["projects"])).toBe(true);

    const projects = (await (await fetch(`${base}/api/projects`)).json()) as unknown[];
    expect(Array.isArray(projects)).toBe(true);
  }, 30_000);
});
