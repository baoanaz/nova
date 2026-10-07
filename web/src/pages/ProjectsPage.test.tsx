/**
 * TASK-094 前端验收（TASK-100 迁移到项目页）：§A 占用列 / §B4 配额条 / §D 删除入口。
 *
 * 迁移说明（TASK-100 §需求4）：项目表与配额条从 `DashboardPage` 移到 `ProjectsPage`
 * （用户要求"新增一个与控制台同级别的页面叫项目"）。**测试内容一字未删**，
 * 只把渲染目标从 `DashboardPage` 换成 `ProjectsPage`——四条纪律照旧：
 *
 * 1. **§D 取消不发请求**：断言 `fetch` 里**没有** DELETE——这是最容易写错的一条
 *    （把删除直接挂在按钮上就会漏掉确认）。
 * 2. **§D 确认才删 + 删完刷新**：断言 DELETE 被调、且之后重新拉了列表（项目消失）。
 * 3. **§D 失败如实报错**：404 不静默吞掉，且**不把整页换成错误页**（项目表仍在）。
 * 4. **§A 口径诚实**：后端未给 `diskBytes` → `—`（不伪装成 0 B）。
 *
 * 渲染一律经 `MemoryRouter`：页面在空态里用了 `<Link>`（跳到接入指南），
 * 没有 Router 上下文会直接抛错——那是测试环境缺件，不是产品缺陷。
 */

import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ProjectsPage } from "./ProjectsPage";

/** 渲染页面（带 Router：页面在空态与提示里用了 `<Link>`）。 */
function renderPage(node: React.ReactElement) {
  return render(<MemoryRouter>{node}</MemoryRouter>);
}

/** 一个带 `diskBytes` 的项目（§A 的字段）。 */
const PROJECT = {
  projectId: "p1",
  displayName: "demo",
  attachedRoot: "/repo/demo",
  indexProgress: { state: "done" },
  sync: { filesIndexed: 287, chunks: 3416 },
  diskBytes: 30_408_704, // 29.0 MiB
};

type Route = { status?: number; body?: unknown };
type Call = { url: string; method: string };

/** 极简 fetch 桩：按 URL 片段匹配；未匹配 → 404。记录方法以便断言"没有发 DELETE"。 */
function stubFetch(routes: Record<string, Route>) {
  const calls: Call[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input.toString();
      const method = (init?.method ?? "GET").toUpperCase();
      calls.push({ url, method });
      for (const [key, value] of Object.entries(routes)) {
        if (url.includes(key)) {
          if (method === "DELETE" && value.status === 204) {
            return new Response(null, { status: 204 });
          }
          return new Response(JSON.stringify(value.body ?? null), { status: value.status ?? 200 });
        }
      }
      return new Response(JSON.stringify({ error: { code: "not_found", message: "no route" } }), {
        status: 404,
      });
    }),
  );
  return calls;
}

function overview(overrides: Record<string, unknown> = {}) {
  return {
    account: { name: "owner", createdAt: 1789300000, isLocal: true, projectCount: 1 },
    index: {
      total: 1,
      succeeded: 1,
      failed: 0,
      avgDurationMs: 1000,
      minDurationMs: 800,
      maxDurationMs: 1200,
      lastRunAt: 1789305566,
      lastState: "done",
      diskBytes: 30_408_704,
      recent: [],
    },
    usage: {
      projectId: null,
      days: 30,
      total: 1,
      succeeded: 1,
      insufficient: 0,
      failed: 0,
      avgLatencyMs: 12,
      p95LatencyMs: 12,
      confidenceDistribution: {},
      citationCoverageAvg: null,
      topQueries: [],
      recent: [],
    },
    projects: [PROJECT],
    days: 30,
    ...overrides,
  };
}

afterEach(() => {
  vi.unstubAllGlobals();
});

// --------------------------------------------------------------------------- §A 占用列

describe("§A 项目占用可见", () => {
  it("项目表格显示每项目占用（复用 formatBytes 的 MiB 口径）", async () => {
    stubFetch({ "/api/account/overview": { body: overview() } });

    renderPage(<ProjectsPage />);

    const cell = await screen.findByTestId("disk-p1");
    expect(cell).toHaveTextContent("29.0 MiB");
    expect(screen.getByRole("columnheader", { name: "占用" })).toBeInTheDocument();
  });

  it("后端未提供 diskBytes 时显示 —（不把未测量伪装成 0 B）", async () => {
    stubFetch({
      "/api/account/overview": {
        body: overview({ projects: [{ ...PROJECT, diskBytes: null }] }),
      },
    });

    renderPage(<ProjectsPage />);

    const cell = await screen.findByTestId("disk-p1");
    expect(cell).toHaveTextContent("—");
    expect(cell).not.toHaveTextContent("0 B");
  });

  it("diskBytes 未提供时也不显示 0 B（旧版服务的兼容路径）", async () => {
    const withoutDisk = { ...PROJECT };
    delete (withoutDisk as { diskBytes?: number }).diskBytes;
    stubFetch({ "/api/account/overview": { body: overview({ projects: [withoutDisk] }) } });

    renderPage(<ProjectsPage />);

    expect(await screen.findByTestId("disk-p1")).toHaveTextContent("—");
  });
});

// --------------------------------------------------------------------------- §B4 配额条

describe("§B4 存储配额展示", () => {
  it("有上限时显示已用/上限与百分比", async () => {
    stubFetch({
      "/api/account/overview": {
        body: overview({
          storage: {
            status: "warning",
            warnRatio: 0.8,
            projectId: "p1",
            user: {
              usedBytes: 1_610_612_736,
              limitBytes: 2_147_483_648,
              ratio: 0.75,
              status: "warning",
              unlimited: false,
            },
            project: { usedBytes: 0, limitBytes: 524_288_000, ratio: 0, status: "ok", unlimited: false },
          },
        }),
      },
    });

    renderPage(<ProjectsPage />);

    const quota = await screen.findByTestId("storage-quota");
    expect(quota).toHaveTextContent("1.50 GiB");
    expect(quota).toHaveTextContent("2.00 GiB");
    expect(quota).toHaveTextContent("75%");
  });

  it("不限（limitBytes=0）时只显示已用，不显示百分比与上限字样", async () => {
    stubFetch({
      "/api/account/overview": {
        body: overview({
          storage: {
            status: "ok",
            warnRatio: 0.8,
            projectId: "p1",
            user: {
              usedBytes: 29 * 1024 * 1024,
              limitBytes: 0,
              ratio: null,
              status: "ok",
              unlimited: true,
            },
            project: { usedBytes: 0, limitBytes: 0, ratio: null, status: "ok", unlimited: true },
          },
        }),
      },
    });

    renderPage(<ProjectsPage />);

    const quota = await screen.findByTestId("storage-quota");
    expect(quota).toHaveTextContent("未设上限");
    expect(quota).not.toHaveTextContent("上限 0 B");
  });

  it("后端未提供 storage 时不渲染配额条（旧版服务）", async () => {
    stubFetch({ "/api/account/overview": { body: overview() } });

    renderPage(<ProjectsPage />);

    await screen.findByTestId("disk-p1");
    expect(screen.queryByTestId("storage-quota")).not.toBeInTheDocument();
  });

  // TASK-100 §需求4 新增：单项目上限用**说明文字**（不是进度条）。
  it("单项目上限以说明文字呈现（用户指定的信息层级）", async () => {
    stubFetch({
      "/api/account/overview": {
        body: overview({
          storage: {
            status: "ok",
            warnRatio: 0.8,
            projectId: "p1",
            user: {
              usedBytes: 29 * 1024 * 1024,
              limitBytes: 2_147_483_648,
              ratio: 0.01,
              status: "ok",
              unlimited: false,
            },
            project: { usedBytes: 0, limitBytes: 524_288_000, ratio: 0, status: "ok", unlimited: false },
          },
        }),
      },
    });

    renderPage(<ProjectsPage />);

    const quota = await screen.findByTestId("storage-quota");
    expect(quota).toHaveTextContent("单个项目上限");
    expect(quota).toHaveTextContent("500.0 MiB");
  });
});

// --------------------------------------------------------------------------- §D 删除入口

describe("§D 项目删除入口", () => {
  it("点击删除弹出二次确认，文案说清后果，且此刻还没有发 DELETE", async () => {
    const calls = stubFetch({ "/api/account/overview": { body: overview() } });
    const user = userEvent.setup();

    renderPage(<ProjectsPage />);
    await screen.findByTestId("disk-p1");

    await user.click(screen.getByRole("button", { name: "删除" }));

    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText(/删除项目 demo/)).toBeInTheDocument();
    expect(within(dialog).getByText(/全部索引数据/)).toBeInTheDocument();
    expect(within(dialog).getByText(/源码文件不受影响/)).toBeInTheDocument();
    // 关键：确认之前绝不能已经删了。
    expect(calls.filter((call) => call.method === "DELETE")).toHaveLength(0);
  });

  it("取消 → 不发请求（断言无 DELETE）", async () => {
    const calls = stubFetch({ "/api/account/overview": { body: overview() } });
    const user = userEvent.setup();

    renderPage(<ProjectsPage />);
    await screen.findByTestId("disk-p1");

    await user.click(screen.getByRole("button", { name: "删除" }));
    await screen.findByRole("dialog");
    await user.click(screen.getByRole("button", { name: "取消" }));

    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    expect(calls.filter((call) => call.method === "DELETE")).toHaveLength(0);
    expect(calls.filter((call) => call.method === "GET")).toHaveLength(1); // 只拉过一次概览
    // 项目行仍在（没有本地删行）。
    expect(screen.getByTestId("disk-p1")).toBeInTheDocument();
  });

  it("确认 → 调 DELETE /api/projects/{id}，并刷新列表使该项目消失", async () => {
    const calls = stubFetch({
      "/api/account/overview": { body: overview() },
      "/api/projects/p1": { status: 204 },
    });
    const user = userEvent.setup();

    // 模拟后端状态：一旦收到 DELETE，后续概览就返回空列表（"真的删掉了"）。
    const empty = overview({ projects: [], account: { ...overview().account, projectCount: 0 } });
    let deleted = false;
    let overviewCalls = 0;
    const original = globalThis.fetch;
    globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input.toString();
      const method = (init?.method ?? "GET").toUpperCase();
      if (url.includes("/api/account/overview")) {
        overviewCalls += 1;
        if (deleted) {
          return new Response(JSON.stringify(empty), { status: 200 });
        }
      }
      if (method === "DELETE") {
        deleted = true;
      }
      return original(input as RequestInfo, init);
    }) as typeof fetch;

    renderPage(<ProjectsPage />);
    await screen.findByTestId("disk-p1");
    const before = overviewCalls;

    await user.click(screen.getByRole("button", { name: "删除" }));
    await screen.findByRole("dialog");
    await user.click(screen.getByRole("button", { name: "删除索引数据" }));

    await waitFor(() => {
      expect(screen.getByText(/还没有项目/)).toBeInTheDocument();
    });
    const deletes = calls.filter((call) => call.method === "DELETE");
    expect(deletes).toHaveLength(1);
    expect(deletes[0]?.url).toContain("/api/projects/p1");
    // 删除后**重新拉取**（不是本地滤掉）：概览请求次数确实增加了。
    expect(overviewCalls).toBeGreaterThan(before);
  });

  it("删除失败（404）→ 如实报错，不静默吞掉，且页面其余部分仍可用", async () => {
    stubFetch({
      "/api/account/overview": { body: overview() },
      "/api/projects/p1": {
        status: 404,
        body: { error: { code: "project_not_found", message: "项目不存在：p1" } },
      },
    });
    const user = userEvent.setup();

    renderPage(<ProjectsPage />);
    await screen.findByTestId("disk-p1");

    await user.click(screen.getByRole("button", { name: "删除" }));
    await screen.findByRole("dialog");
    await user.click(screen.getByRole("button", { name: "删除索引数据" }));

    await waitFor(() => {
      expect(screen.getByText("项目不存在：p1")).toBeInTheDocument();
    });
    expect(screen.getByText("project_not_found")).toBeInTheDocument();
    // 不是整页错误：项目表仍在，用户可以重试或取消。
    expect(screen.getByTestId("disk-p1")).toBeInTheDocument();
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });

  it("权限/网络类错误也如实展示（不静默）", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = typeof input === "string" ? input : input.toString();
        if (url.includes("/api/account/overview")) {
          return new Response(JSON.stringify(overview()), { status: 200 });
        }
        if ((init?.method ?? "GET").toUpperCase() === "DELETE") {
          throw new TypeError("Failed to fetch");
        }
        return new Response("null", { status: 200 });
      }),
    );
    const user = userEvent.setup();

    renderPage(<ProjectsPage />);
    await screen.findByTestId("disk-p1");
    await user.click(screen.getByRole("button", { name: "删除" }));
    await screen.findByRole("dialog");
    await user.click(screen.getByRole("button", { name: "删除索引数据" }));

    await waitFor(() => {
      expect(screen.getByText(/连不上 nova-service/)).toBeInTheDocument();
    });
  });
});
