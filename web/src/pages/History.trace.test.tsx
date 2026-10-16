/**
 * 历史页（TASK-094 §C / TASK-100 §需求2，随「历史记录重构」改为：列表 + 汇总卡 + 详情弹窗）。
 * trace id 与阶段耗时在详情弹窗里；列表只保留时间 / 类型 / 项目 / 内容 / 结果 / 耗时 / 体量。
 *
 * 迁移说明（TASK-100）：历史页从"索引记录 / 使用记录两个页签"合并成**一张表**
 * （用户 2026-09-14 要求），因此不再需要点页签；断言内容**一条未删**，只调整了定位方式：
 *
 * | 原断言 | 现断言 | 守护的东西 |
 * |---|---|---|
 * | 点「使用记录」页签后查表 | 直接查表（已合并） | 检索记录可见 |
 * | `getByText("trace id")` 表头 | 保留 | trace id 列存在 |
 * | 复制按钮 + "报给管理员" 文案 | 改为"复制"+ 弹窗里的 trace id 复制 | 可复制的 trace id |
 * | 无 requestId → `—` | 保留 | 不编造 id |
 *
 * 另外新增（TASK-100 §需求2 的核心）：类型列区分仓库初始化 / 检索，
 * 以及"查看"按钮弹出输入输出详情。
 */

import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { HistoryPage } from "./HistoryPage";

function renderPage(node: React.ReactElement) {
  return render(<MemoryRouter>{node}</MemoryRouter>);
}

const PROJECT = {
  projectId: "p1",
  displayName: "demo",
  attachedRoot: "/repo/demo",
  indexProgress: { state: "done" },
  sync: { filesIndexed: 287, chunks: 3416 },
  diskBytes: 30_408_704,
};

/**
 * 时间基：历史页按 ``Date.now() - days * 86400`` 过滤窗口（HistoryPage「合并 + 按时间倒序 +
 * 按窗口过滤」），因此夹具**必须给相对时间**。
 *
 * 回归锚点（真实事故）：夹具原先是写死的 ``1789305500/1789305566``（= 2026-09-13）。
 * 2026-09-18 的 CI 是绿的，到 2026-09-26 就红了——「近 7 天」窗口滑过了夹具时间，
 * 表格变成空态，``findByRole("table")`` 找不到。**与代码改动无关，是夹具的自然过期**。
 */
const NOW = Math.floor(Date.now() / 1000);

const RUN = {
  runId: 7,
  projectId: "p1",
  state: "done",
  startedAt: NOW - 66,
  finishedAt: NOW,
  durationMs: 66_000,
  filesTotal: 344,
  filesProcessed: 338,
  chunks: 4210,
  errors: 0,
  error: null,
};

const record = {
  queryId: 1,
  projectId: "p1",
  mode: "fast",
  query: "令牌在哪里刷新",
  answerable: true,
  confidence: "high",
  degraded: false,
  latencyMs: 137,
  evidenceCount: 3,
  docsCount: 1,
  usedTokens: 576,
  citationCoverage: null,
  requestId: "trace-094-abcdef",
  createdAt: NOW,
};

function usage(records: unknown[]) {
  return {
    projectId: null,
    days: 30,
    total: records.length,
    succeeded: records.length,
    insufficient: 0,
    failed: 0,
    avgLatencyMs: 137,
    p95LatencyMs: 137,
    confidenceDistribution: {},
    citationCoverageAvg: null,
    topQueries: [],
    recent: records,
  };
}

/** 统一的 fetch 桩：项目列表 + 索引 run + 用量。 */
function stubAll(records: unknown[] = [record], runs: unknown[] = [RUN]) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url.includes("/index-runs")) {
        return new Response(JSON.stringify(runs), { status: 200 });
      }
      if (url.includes("/api/projects")) {
        return new Response(JSON.stringify([PROJECT]), { status: 200 });
      }
      return new Response(JSON.stringify(usage(records)), { status: 200 });
    }),
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

/** 打开某行的详情弹窗。 */
async function openDetail(match: string) {
  const table = await screen.findByRole("table");
  const row = within(table)
    .getAllByRole("row")
    .find((item) => item.textContent?.includes(match))!;
  await userEvent.click(within(row).getByRole("button", { name: "详情" }));
  return screen.findByRole("dialog");
}

describe("§C 历史页 trace id（TASK-094）", () => {
  it("检索记录的详情里显示可复制的 trace id", async () => {
    stubAll();
    renderPage(<HistoryPage />);

    const dialog = await openDetail("令牌在哪里刷新");
    expect(within(dialog).getByText("trace id")).toBeInTheDocument();
    expect(within(dialog).getByText("trace-094-abcdef")).toBeInTheDocument();
    expect(within(dialog).getAllByRole("button", { name: "复制" }).length).toBeGreaterThanOrEqual(1);
  });

  it("旧记录没有 requestId → 不显示 trace id 行，不编造一个 id", async () => {
    stubAll([{ ...record, requestId: null }]);
    renderPage(<HistoryPage />);

    const dialog = await openDetail("令牌在哪里刷新");
    expect(within(dialog).queryByText("trace id")).not.toBeInTheDocument();
    expect(within(dialog).queryByText(/trace-094/)).not.toBeInTheDocument();
  });
});

describe("§需求2 合并表格（TASK-100）", () => {
  it("一张表同时包含「仓库初始化」与「检索」，用类型列区分", async () => {
    stubAll();
    renderPage(<HistoryPage />);

    const table = await screen.findByRole("table");
    // 两种记录都在同一张表里（这是用户要求的核心：不用切页签）。
    // TASK-100（用户 2026-09-14）：「类型」列改为显示**实际工具名**。
    expect(within(table).getByText("仓库初始化")).toBeInTheDocument();
    expect(within(table).getByText("search")).toBeInTheDocument();
    // 项目与内容分列。
    expect(within(table).getByText("项目")).toBeInTheDocument();
    expect(within(table).getByText("内容")).toBeInTheDocument();
    // 两行都属于 demo 项目，因此项目名出现两次（这正是分列后的正确表现）。
    expect(within(table).getAllByText("demo").length).toBe(2);
    expect(within(table).getByText("令牌在哪里刷新")).toBeInTheDocument();
    // 体量：初始化看 chunks，检索看 token。
    expect(within(table).getByText("4210 chunks")).toBeInTheDocument();
    expect(within(table).getByText("576 token")).toBeInTheDocument();
  });

  it("仓库初始化那行的「内容」列写文件数与 chunks，而不是空着", async () => {
    stubAll();
    renderPage(<HistoryPage />);

    const table = await screen.findByRole("table");
    const initRow = within(table)
      .getAllByRole("row")
      .find((row) => row.textContent?.includes("仓库初始化"))!;
    const cells = within(initRow).getAllByRole("cell");
    // 列序：时间 0 / 类型 1 / 项目 2 / 内容 3 / 结果 4 / 耗时 5 / 体量 6 / 详情 7
    expect(cells[2]).toHaveTextContent("demo");
    expect(cells[3]).toHaveTextContent("338 / 344 个文件 · 4210 chunks");
  });

  it("按时间倒序：较新的记录排在前面", async () => {
    // 初始化与检索同秒会让顺序不确定——把它改早一点（仍是相对时间，见 NOW 的说明）。
    stubAll([record], [{ ...RUN, finishedAt: NOW - 566 }]);
    renderPage(<HistoryPage />);

    const table = await screen.findByRole("table");
    const rows = within(table).getAllByRole("row").slice(1); // 去掉表头
    expect(rows.length).toBe(2);
    // 第一行必须是较新的那条（检索 NOW > 初始化 NOW-566）。
    expect(within(rows[0]!).getByText("search")).toBeInTheDocument();
    expect(within(rows[1]!).getByText("仓库初始化")).toBeInTheDocument();
  });

  it("「详情」弹出两个可滚动代码块（Tool 输入 / 输出）", async () => {
    stubAll();
    renderPage(<HistoryPage />);

    const dialog = await openDetail("令牌在哪里刷新");
    expect(within(dialog).getByText("Tool 输入")).toBeInTheDocument();
    expect(within(dialog).getByText("Tool 输出")).toBeInTheDocument();
    expect(within(dialog).queryByText("LLM 答案")).not.toBeInTheDocument();
    const pres = dialog.querySelectorAll("pre");
    expect(pres.length).toBe(2);
    expect(pres[0]?.textContent).toContain("令牌在哪里刷新");
    expect(pres[0]?.className).toContain("overflow-auto");
    expect(within(dialog).getByText("信心")).toBeInTheDocument();
    expect(within(dialog).getByText("Token")).toBeInTheDocument();
    expect(within(dialog).getByText("证据（代码/文档/测试）")).toBeInTheDocument();
    // 诚实边界：检索只有整次耗时，如实说明没有分段。
    expect(within(dialog).getByText(/没有拆分检索 \/ LLM 的分段耗时/)).toBeInTheDocument();
  });

  it("弹窗标题不重复输入内容（用户：太长了）", async () => {
    stubAll();
    renderPage(<HistoryPage />);

    const dialog = await openDetail("令牌在哪里刷新");
    const heading = within(dialog).getByRole("heading");
    expect(heading.textContent).toContain("search");
    expect(heading.textContent).toContain("demo");
    expect(heading.textContent).not.toContain("令牌在哪里刷新");
  });

  it("弹窗可关闭", async () => {
    stubAll();
    renderPage(<HistoryPage />);

    await openDetail("令牌在哪里刷新");
    await userEvent.click(screen.getByRole("button", { name: "关闭" }));
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
  });
});

describe("汇总卡与筛选", () => {
  const ASK = { ...record, queryId: 2, query: "登录流程是什么", answerText: "见 auth.py", answerStatus: "answered" };

  it("三类各一张汇总卡，ask 与 search 分开计数", async () => {
    stubAll([record, ASK]);
    renderPage(<HistoryPage />);

    const group = await screen.findByRole("group", { name: "按类型筛选" });
    const cards = within(group).getAllByRole("button");
    expect(cards.map((card) => card.textContent)).toEqual([
      expect.stringContaining("仓库初始化"),
      expect.stringContaining("search"),
      expect.stringContaining("ask"),
    ]);
    for (const card of cards) expect(card).toHaveTextContent("1次");
  });

  it("点汇总卡按类型筛选，再点取消", async () => {
    stubAll([record, ASK]);
    renderPage(<HistoryPage />);

    const group = await screen.findByRole("group", { name: "按类型筛选" });
    await userEvent.click(within(group).getAllByRole("button")[2]!);
    const table = screen.getByRole("table");
    expect(within(table).getAllByRole("row").slice(1)).toHaveLength(1);
    expect(within(table).getByText("登录流程是什么")).toBeInTheDocument();

    await userEvent.click(within(group).getAllByRole("button")[2]!);
    expect(within(screen.getByRole("table")).getAllByRole("row").slice(1)).toHaveLength(3);
  });

  it("「只看异常」隐藏成功的记录", async () => {
    stubAll([record, { ...record, queryId: 3, query: "坏掉的问题", answerable: null, degraded: true }]);
    renderPage(<HistoryPage />);

    await screen.findByRole("table");
    await userEvent.click(screen.getByRole("button", { name: "只看异常" }));
    const rows = within(screen.getByRole("table")).getAllByRole("row").slice(1);
    expect(rows).toHaveLength(1);
    expect(rows[0]).toHaveTextContent("坏掉的问题");
  });
});

describe("初始化的分阶段耗时", () => {
  const STAGES = [
    { name: "engine_open", wallMs: 400, cpuMs: 10 },
    { name: "collect", wallMs: 1000, cpuMs: 900 },
    { name: "write_batch", wallMs: 6000, cpuMs: 1500 },
    { name: "prepare", wallMs: 2500, cpuMs: 2400 },
    { name: "persist", wallMs: 1800, cpuMs: 800 },
    { name: "graph", wallMs: 500, cpuMs: 480 },
    { name: "vector_drain", wallMs: 100, cpuMs: 1 },
  ];

  it("有阶段数据：按顶层阶段列出毫秒与占比，嵌套阶段缩进在「解析并写入索引」下", async () => {
    stubAll([], [{ ...RUN, durationMs: 8000, elapsedMs: 8400, stages: STAGES }]);
    renderPage(<HistoryPage />);

    const dialog = await openDetail("仓库初始化");
    for (const label of ["打开项目", "收集待索引文件", "解析并写入索引", "建立调用关系图", "解析与切块", "写入存储"]) {
      expect(within(dialog).getByText(label)).toBeInTheDocument();
    }
    // 总耗时取实测毫秒（8400），不是整秒的 8000。
    expect(dialog.textContent).toContain("8.4 s");
    // 原始阶段表可展开对账。
    expect(within(dialog).getByText("原始阶段数据（墙钟 / CPU）")).toBeInTheDocument();
  });

  it("没有阶段数据（旧记录）→ 如实说明，不画空瀑布", async () => {
    stubAll([], [RUN]);
    renderPage(<HistoryPage />);

    const dialog = await openDetail("仓库初始化");
    expect(within(dialog).getByText(/没有分阶段数据/)).toBeInTheDocument();
    expect(within(dialog).queryByText("打开项目")).not.toBeInTheDocument();
  });
});

describe("§需求3 时间范围选择（TASK-100）", () => {
  it("提供天为单位的档位，默认近 30 天", async () => {
    stubAll();
    renderPage(<HistoryPage />);

    // 默认档位高亮。
    const active = await screen.findByRole("button", { name: "近 30 天" });
    expect(active.className).toContain("bg-accent-seal");
    // 其余档位存在。
    expect(screen.getByRole("button", { name: "今天" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "近 7 天" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "近 90 天" })).toBeInTheDocument();
  });

  it("切换档位会带 days 重新请求检索汇总（后端参数）", async () => {
    const calls: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = typeof input === "string" ? input : input.toString();
        calls.push(url);
        if (url.includes("/index-runs")) {
          return new Response(JSON.stringify([RUN]), { status: 200 });
        }
        if (url.includes("/api/projects")) {
          return new Response(JSON.stringify([PROJECT]), { status: 200 });
        }
        return new Response(JSON.stringify(usage([record])), { status: 200 });
      }),
    );

    renderPage(<HistoryPage />);
    await screen.findByRole("table");

    await userEvent.click(screen.getByRole("button", { name: "近 7 天" }));

    await waitFor(() => {
      expect(calls.some((url) => url.includes("days=7"))).toBe(true);
    });
  });
});

describe("§需求4 刷新（TASK-100 用户 2026-09-14 定稿）", () => {
  it("页头排版：标题 + 刷新按钮 + 数据更新于（同一个左侧分组）", async () => {
    stubAll();
    renderPage(<HistoryPage />);

    await screen.findByRole("table");
    expect(screen.getByRole("button", { name: "刷新" })).toBeInTheDocument();
    // 首次加载成功后显示时间戳（用户要求短格式「数据更新于：HH:MM:SS」）。
    expect(await screen.findByText(/数据更新于：/)).toBeInTheDocument();
  });

  it("自动刷新常开且**不提供开关**（用户定稿：不让用户管这个概念）", async () => {
    stubAll();
    renderPage(<HistoryPage />);

    await screen.findByRole("table");
    // 页面上不应再有 switch 控件或「自动刷新」字样。
    expect(screen.queryByRole("switch")).not.toBeInTheDocument();
    expect(screen.queryByText("自动刷新")).not.toBeInTheDocument();
  });

  it("刷新按钮点击后重新请求（手动刷新仍可用）", async () => {
    const calls: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = typeof input === "string" ? input : input.toString();
        calls.push(url);
        if (url.includes("/index-runs")) {
          return new Response(JSON.stringify([RUN]), { status: 200 });
        }
        if (url.includes("/api/projects")) {
          return new Response(JSON.stringify([PROJECT]), { status: 200 });
        }
        return new Response(JSON.stringify(usage([record])), { status: 200 });
      }),
    );

    renderPage(<HistoryPage />);
    await screen.findByRole("table");
    const before = calls.filter((url) => url.includes("usage/summary")).length;

    await userEvent.click(screen.getByRole("button", { name: "刷新" }));

    await waitFor(() => {
      const after = calls.filter((url) => url.includes("usage/summary")).length;
      expect(after).toBeGreaterThan(before);
    });
  });

  /**
   * **用户报的\"低概率全白\"的回归用例**（2026-09-14）：
   * 切时间档位时旧表格必须留在屏幕上，不能被 LoadingBlock 换掉。
   *
   * 根因是首屏 effect 一律走非静默路径 → `runs`/`usage` 被置空 → `rows` 为 null →
   * 渲染 LoadingBlock。修复后 `load` 按\"屏幕上是否已有数据\"决定静默与否。
   */
  it("切时间档位时不白屏：表格在整个切换过程中都留在 DOM 里", async () => {
    stubAll();
    renderPage(<HistoryPage />);

    const table = await screen.findByRole("table");
    expect(table).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "近 7 天" }));

    // 切换过程中**任何时刻**都不应出现加载态（表格必须一直在）。
    // 用 waitFor 循环观察：若中途被 LoadingBlock 替换，这里会抓到。
    let sawLoading = false;
    const observer = setInterval(() => {
      if (screen.queryByText("加载中…") !== null) sawLoading = true;
    }, 1);
    await waitFor(() => {
      expect(screen.getByRole("table")).toBeInTheDocument();
    });
    clearInterval(observer);
    expect(sawLoading).toBe(false);
    expect(screen.queryByText("加载中…")).not.toBeInTheDocument();
  });

  it("刷新失败时不清空已有数据（只显示错误横幅）", async () => {
    let failNext = false;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = typeof input === "string" ? input : input.toString();
        if (url.includes("usage/summary") && failNext) {
          throw new TypeError("Failed to fetch");
        }
        if (url.includes("/index-runs")) {
          return new Response(JSON.stringify([RUN]), { status: 200 });
        }
        if (url.includes("/api/projects")) {
          return new Response(JSON.stringify([PROJECT]), { status: 200 });
        }
        return new Response(JSON.stringify(usage([record])), { status: 200 });
      }),
    );

    renderPage(<HistoryPage />);
    await screen.findByRole("table");

    failNext = true;
    await userEvent.click(screen.getByRole("button", { name: "刷新" }));

    await waitFor(() => {
      expect(screen.getByText(/连不上 nova-service/)).toBeInTheDocument();
    });
    // 表格仍在（清空会让"网络抖一下"看起来像"记录全没了"）。
    expect(screen.getByRole("table")).toBeInTheDocument();
  });
});

describe("§需求5 同一次调用折叠（TASK-100 用户 2026-09-14 提问）", () => {
  /** 同 callId 的三条 run——真实的 113+119+55=287 分批形态。 */
  const BATCHED = [
    { ...RUN, runId: 1, filesProcessed: 113, filesTotal: 113, durationMs: 4000, callId: "call-A" },
    { ...RUN, runId: 2, filesProcessed: 119, filesTotal: 119, durationMs: 2000, callId: "call-A", finishedAt: RUN.finishedAt + 2 },
    { ...RUN, runId: 3, filesProcessed: 55, filesTotal: 55, durationMs: 2000, callId: "call-A", finishedAt: RUN.finishedAt + 4 },
  ];

  it("同 callId 的多条初始化折叠成一行，耗时与文件数求和", async () => {
    // 时间戳可能让三条落进同一秒，这里靠 callId 分组（不靠时间）。
    stubAll([], BATCHED);
    renderPage(<HistoryPage />);

    const table = await screen.findByRole("table");
    const rows = within(table).getAllByRole("row").slice(1);
    // 三条 run 折叠后只剩一行。
    expect(rows.length).toBe(1);
    // 工具名标出批次数（用户能看出它分了几批）。
    expect(within(rows[0]!).getByText("仓库初始化")).toBeInTheDocument();
    expect(rows[0]!.textContent).toContain("287 / 287 个文件");
    // 耗时**求和**：4000+2000+2000 = 8000ms → "8.0 s"（不是单批的 4.0 s）。
    expect(rows[0]!.textContent).toContain("8.0 s");
    expect(rows[0]!.textContent).not.toContain("4.0 s");
  });

  it("无 callId 的记录各自独立展示（不假装它们属于同一次）", async () => {
    const standalone = BATCHED.map((run) => ({ ...run, callId: null }));
    stubAll([], standalone);
    renderPage(<HistoryPage />);

    const table = await screen.findByRole("table");
    // 三条独立记录 → 三行（**不折叠**）。
    expect(within(table).getAllByRole("row").slice(1).length).toBe(3);
  });

  it("详情里列出每一批的文件数与耗时", async () => {
    stubAll([], BATCHED);
    renderPage(<HistoryPage />);

    const dialog = await openDetail("仓库初始化");
    expect(within(dialog).getByText("上传批次（3 批）")).toBeInTheDocument();
    const batchRows = within(dialog).getAllByRole("row").slice(1);
    expect(batchRows).toHaveLength(3);
    expect(batchRows[0]).toHaveTextContent("113 / 113");
    expect(batchRows[0]).toHaveTextContent("4.0 s");
    const input = dialog.querySelectorAll("pre")[0]?.textContent ?? "";
    expect(input).toContain("上传批次：3 批");
  });
});
