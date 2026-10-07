/**
 * TASK-110 P4 验收：三种身份看到的页面确实不同（2026-09-15 按用户要求改版后）。
 *
 * 卡内 §5 的 P4 清单（**逐条一个断言**）：
 *
 * | 验收项 | 用例 |
 * |---|---|
 * | 内测/管理员的创建弹窗里能看到自定义行 | ``describe("创建 Key 弹窗")`` |
 * | 公测用户的弹窗里**没有**这一行 | 同上第二条 |
 * | 头衔与编号在控制台可见（`ID #001` / `拓荒者 #027`） | ``describe("控制台账户卡")`` |
 * | 三种身份登录后页面确实不同 | ``describe("三身份对照")`` |
 *
 * 测试策略：把 ``fetch`` 桩成按路由返回不同身份（与 ``App.test.tsx`` 同一手法），
 * 然后**渲染真实页面组件**并断言可见文本。不断言实现细节（class 名），只断言
 * "用户能看到什么"——那才是"页面确实不同"的可观察证据。
 */

import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { Account } from "../api/client";
import { AdminPage } from "./AdminPage";
import { ApiKeysPage } from "./ApiKeysPage";
import { DashboardPage } from "./DashboardPage";

/** 一个账户模板（身份字段由用例覆盖；能力位必须与角色**一致**，它们本就是同源的）。 */
const PUBLIC_ACCOUNT: Account = {
  userId: "u1",
  name: "plain",
  createdAt: 1789300000,
  isLocal: false,
  via: "session",
  role: "public",
  title: "旅人",
  earlyMemberNo: null,
  userNo: 12,
  capabilities: {
    canCustomKey: false,
    quotaBytes: 500 * 1024 * 1024,
    earlyMemberNo: null,
    isAdmin: false,
  },
};

const BETA_ACCOUNT: Account = {
  ...PUBLIC_ACCOUNT,
  name: "early",
  role: "beta",
  title: "拓荒者",
  earlyMemberNo: 27,
  userNo: 3,
  capabilities: {
    canCustomKey: true,
    quotaBytes: 1024 * 1024 * 1024,
    earlyMemberNo: 27,
    isAdmin: false,
  },
};

const ADMIN_ACCOUNT: Account = {
  ...PUBLIC_ACCOUNT,
  name: "boss",
  role: "admin",
  title: "执炬者",
  userNo: 1,
  capabilities: {
    canCustomKey: true,
    quotaBytes: 5 * 1024 * 1024 * 1024,
    earlyMemberNo: null,
    isAdmin: true,
  },
};

/** 按路由前缀桩 ``fetch``（返回体由调用方给）。 */
function stubRoutes(routes: Record<string, unknown>, status = 200) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : input.toString();
      for (const [key, body] of Object.entries(routes)) {
        if (url.includes(key)) {
          return new Response(JSON.stringify(body), { status });
        }
      }
      return new Response(JSON.stringify({ error: { code: "not_found", message: "no route" } }), {
        status: 404,
      });
    }),
  );
}

function renderInRouter(node: React.ReactNode) {
  return render(<MemoryRouter initialEntries={["/"]}>{node}</MemoryRouter>);
}

afterEach(() => {
  vi.unstubAllGlobals();
});

// --------------------------------------------------------------------------- 创建 Key 弹窗

describe("创建 Key 弹窗（TASK-110 §1.7 改版）", () => {
  it("页面只有列表卡片，弹窗要点「创建 Key」才出现", async () => {
    stubRoutes({ "/api/auth/me": PUBLIC_ACCOUNT, "/api/auth/tokens": [] });
    const user = userEvent.setup();
    renderInRouter(<ApiKeysPage />);

    // 列表先加载出来（空态可操作）。
    expect(await screen.findByText("现有的 Key")).toBeInTheDocument();
    // 弹窗内容初始不在页面上。
    expect(screen.queryByRole("dialog")).toBeNull();

    await user.click(screen.getByRole("button", { name: "创建 Key" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("创建 API Key")).toBeInTheDocument();
    expect(within(dialog).getByLabelText(/名称/)).toBeInTheDocument();
  });

  it("内测用户弹窗里有「🧭 拓荒者特权 · 可自定义 Key，必须以 nova_ 开头」", async () => {
    stubRoutes({ "/api/auth/me": BETA_ACCOUNT, "/api/auth/tokens": [] });
    const user = userEvent.setup();
    renderInRouter(<ApiKeysPage />);

    await screen.findByText("现有的 Key");
    await user.click(screen.getByRole("button", { name: "创建 Key" }));
    const dialog = await screen.findByRole("dialog");

    expect(within(dialog).getByText("🧭 拓荒者特权")).toBeInTheDocument();
    expect(within(dialog).getByText(/可自定义 Key，必须以 nova_ 开头/)).toBeInTheDocument();
    expect(within(dialog).getByPlaceholderText("nova_my-laptop-key-2026")).toBeInTheDocument();
  });

  it("管理员弹窗里显示的是「执炬者特权」", async () => {
    stubRoutes({ "/api/auth/me": ADMIN_ACCOUNT, "/api/auth/tokens": [] });
    const user = userEvent.setup();
    renderInRouter(<ApiKeysPage />);

    await screen.findByText("现有的 Key");
    await user.click(screen.getByRole("button", { name: "创建 Key" }));
    const dialog = await screen.findByRole("dialog");

    expect(within(dialog).getByText("🧭 执炬者特权")).toBeInTheDocument();
  });

  it("公测用户弹窗里**没有**自定义行（真的不渲染，不是置灰）", async () => {
    stubRoutes({ "/api/auth/me": PUBLIC_ACCOUNT, "/api/auth/tokens": [] });
    const user = userEvent.setup();
    renderInRouter(<ApiKeysPage />);

    await screen.findByText("现有的 Key");
    await user.click(screen.getByRole("button", { name: "创建 Key" }));
    const dialog = await screen.findByRole("dialog");

    // 名称行在、自定义行不在（用户要求"普通用户看不见这个 key 行"）。
    expect(within(dialog).getByLabelText(/名称/)).toBeInTheDocument();
    expect(within(dialog).queryByText(/特权/)).toBeNull();
    expect(within(dialog).queryByPlaceholderText("nova_my-laptop-key-2026")).toBeNull();
  });

  it("创建成功后弹窗展示明文并提供复制", async () => {
    stubRoutes({
      "/api/auth/me": BETA_ACCOUNT,
      "/api/auth/tokens": [{ id: "k1", name: "laptop", prefix: "nova_my-lap", createdAt: 1, lastUsedAt: null, isCustom: true }],
    });
    // 创建接口的响应要带明文；用独立的 fetch 桩覆盖。
    const created = { id: "k1", token: "nova_mytest", prefix: "nova_mytest", name: "laptop", isCustom: true };
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = typeof input === "string" ? input : input.toString();
        if (url.includes("/api/auth/tokens") && init?.method === "POST") {
          return new Response(JSON.stringify(created), { status: 200 });
        }
        if (url.includes("/api/auth/me")) {
          return new Response(JSON.stringify(BETA_ACCOUNT), { status: 200 });
        }
        return new Response(JSON.stringify([]), { status: 200 });
      }),
    );

    const user = userEvent.setup();
    renderInRouter(<ApiKeysPage />);
    await screen.findByText("现有的 Key");
    await user.click(screen.getByRole("button", { name: "创建 Key" }));
    await user.click(await screen.findByRole("button", { name: "创建" }));

    // 结果视图：明文可见 + 有复制按钮。
    expect(await screen.findByText("nova_mytest")).toBeInTheDocument();
    expect(screen.getByText(/唯一一次/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /复制 Key/ })).toBeInTheDocument();
  });
});

// --------------------------------------------------------------------------- 控制台账户卡

describe("控制台账户卡（账户页已合并进来；TASK-110）", () => {
  /** 账户概览的最小响应（账户卡只需要 account 段）。 */
  const OVERVIEW = {
    account: { name: "early", createdAt: 1789300000, isLocal: false, projectCount: 2 },
    index: {
      total: 0,
      succeeded: 0,
      failed: 0,
      avgDurationMs: null,
      minDurationMs: null,
      maxDurationMs: null,
      lastRunAt: null,
      lastState: null,
      recent: [],
    },
    usage: {
      total: 0,
      succeeded: 0,
      insufficient: 0,
      failed: 0,
      avgLatencyMs: null,
      p95LatencyMs: null,
      confidenceDistribution: {},
      citationCoverageAvg: null,
      topQueries: [],
      recent: [],
    },
    projects: [],
    days: 30,
  };

  it("显示 ID #012（全站顺序号）与身份徽章 拓荒者 #027", async () => {
    stubRoutes({
      "/api/auth/me": { ...BETA_ACCOUNT, userNo: 12 },
      "/api/account/overview": OVERVIEW,
      "/api/meta": { version: "0.0.1", localMode: false, config: { embedding: {}, llm: {} } },
    });
    renderInRouter(<DashboardPage />);

    expect(await screen.findByRole("heading", { name: "账户资料" })).toBeInTheDocument();
    expect(screen.getByText("#012")).toBeInTheDocument();
    expect(screen.getByTestId("title-badge")).toHaveTextContent("拓荒者 #027");
    // 特权与额度也在同一张卡里（用户要求"合并进控制台"）。
    expect(screen.getByText(/可用（拓荒者特权）/)).toBeInTheDocument();
    expect(screen.getByText("1.00 GiB")).toBeInTheDocument();
  });

  it("公测用户显示 旅人 与 拓荒者专属", async () => {
    stubRoutes({
      "/api/auth/me": PUBLIC_ACCOUNT,
      "/api/account/overview": OVERVIEW,
      "/api/meta": { version: "0.0.1", localMode: false, config: { embedding: {}, llm: {} } },
    });
    renderInRouter(<DashboardPage />);

    await screen.findByRole("heading", { name: "账户资料" });
    expect(screen.getByTestId("title-badge")).toHaveTextContent("旅人");
    expect(screen.getByText("拓荒者专属")).toBeInTheDocument();
    expect(screen.getByText("500.0 MiB")).toBeInTheDocument();
  });
});

// --------------------------------------------------------------------------- 后台页

describe("管理员后台（TASK-110 §1.6 改版）", () => {
  it("四个标签：用户 / 邀请码 / 信息查询 / 系统状态", async () => {
    stubRoutes({ "/api/admin/users": { users: [], limit: 100 } });
    renderInRouter(<AdminPage />);

    await waitFor(() => expect(screen.getByText("用户")).toBeInTheDocument());
    for (const label of ["用户", "邀请码", "信息查询", "系统状态"]) {
      expect(screen.getByRole("button", { name: label })).toBeInTheDocument();
    }
    // 旧的「项目」「调用统计」已合并，不应再是两个独立标签。
    expect(screen.queryByRole("button", { name: "调用统计" })).toBeNull();
    expect(screen.queryByRole("button", { name: "项目" })).toBeNull();
  });

  it("用户列表显示 ID 编号、身份与可点的额度", async () => {
    stubRoutes({
      "/api/admin/users": {
        users: [
          {
            userId: "u2",
            name: "early",
            createdAt: 1789300000,
            isLocal: false,
            role: "beta",
            title: "拓荒者",
            earlyMemberNo: 27,
            userNo: 3,
            quotaBytes: null,
            effectiveQuotaBytes: 1024 * 1024 * 1024,
            bannedAt: null,
            lastSeenAt: 1789400000,
            projectCount: 3,
            usedBytes: 1024 * 1024,
            usedText: "1.0 MiB",
            queryCount: 12,
          },
        ],
        limit: 100,
      },
    });
    renderInRouter(<AdminPage />);

    expect(await screen.findByText("early")).toBeInTheDocument();
    expect(screen.getByText(/ID #003/)).toBeInTheDocument();
    expect(screen.getByText("#027")).toBeInTheDocument();
    expect(screen.getByText("1.0 MiB")).toBeInTheDocument();
    // 额度是**可点按钮**（改配额入口）。
    expect(screen.getByRole("button", { name: /1.00 GiB/ })).toBeInTheDocument();
  });

  it("信息查询：项目表显示归属人名称与占用，且有删除按钮", async () => {
    stubRoutes({
      "/api/admin/projects": {
        projects: [
          {
            projectId: "p1",
            displayName: "demo",
            attachedRoot: "/tmp/demo",
            ownerId: "u2",
            ownerName: "early",
            ownerNo: 3,
            diskBytes: 2 * 1024 * 1024,
            indexProgress: null,
            history: { total: 1, succeeded: 1, failed: 0, lastState: "done", lastRunAt: 1789400000 },
            lastError: null,
            lastErrors: 0,
            lastSkipped: 0,
          },
        ],
        totalBytes: 2 * 1024 * 1024,
        owners: [{ userId: "u2", name: "early", userNo: 3, role: "beta" }],
      },
      "/api/admin/stats": {
        days: 30,
        userId: null,
        projectCount: 1,
        search: { total: 5, succeeded: 4, insufficient: 1, failed: 0, avgLatencyMs: 12, p95LatencyMs: 30 },
        index: { total: 1, succeeded: 1, failed: 0, avgDurationMs: 100 },
        errorRate: 0,
        totalQueries: 5,
        tokens: 500,
        owners: [],
      },
    });
    const user = userEvent.setup();
    renderInRouter(<AdminPage />);

    await user.click(screen.getByRole("button", { name: "信息查询" }));

    // 归属人显示**名称**（用户要求：一串编号看不出什么意思）。
    expect(await screen.findByText("early")).toBeInTheDocument();
    expect(screen.getByText("2.0 MiB")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "删除" })).toBeInTheDocument();
    // 统计与项目在同一页（已合并）。
    expect(screen.getByText(/近 30 天/)).toBeInTheDocument();
    expect(screen.getByText("5 次")).toBeInTheDocument();
    // 用户下拉存在（可筛某个用户）。
    expect(screen.getByRole("combobox")).toBeInTheDocument();
    expect(screen.getByRole("option", { name: /early/ })).toBeInTheDocument();
  });

  it("系统状态显示 VPS 内存", async () => {
    stubRoutes({
      "/api/admin/system": {
        status: "ok",
        version: "0.0.1",
        dataRoot: "/tmp/data",
        localMode: false,
        auth: "required",
        core: { importable: true, ok: true },
        projects: [],
        host: {
          totalBytes: 2 * 1024 ** 3,
          availableBytes: 512 * 1024 ** 2,
          usedBytes: 1536 * 1024 ** 2,
          usedRatio: 0.75,
          availableBasis: "MemAvailable",
          reason: null,
        },
      },
    });
    const user = userEvent.setup();
    renderInRouter(<AdminPage />);

    await user.click(screen.getByRole("button", { name: "系统状态" }));

    expect(await screen.findByText("VPS 内存")).toBeInTheDocument();
    expect(screen.getByText("1.50 GiB")).toBeInTheDocument();
    expect(screen.getByText("75.0%")).toBeInTheDocument();
    expect(screen.getByRole("progressbar", { name: "内存使用率" })).toBeInTheDocument();
  });

  it("读不到主机内存时如实说明原因（不显示 0）", async () => {
    stubRoutes({
      "/api/admin/system": {
        status: "ok",
        version: "0.0.1",
        dataRoot: "/tmp/data",
        localMode: false,
        auth: "required",
        core: { importable: true },
        projects: [],
        host: {
          totalBytes: null,
          availableBytes: null,
          usedBytes: null,
          usedRatio: null,
          availableBasis: null,
          reason: "读不到 /proc/meminfo（FileNotFoundError）：仅 Linux 可测",
        },
      },
    });
    const user = userEvent.setup();
    renderInRouter(<AdminPage />);

    await user.click(screen.getByRole("button", { name: "系统状态" }));

    expect(await screen.findByText(/仅 Linux 可测/)).toBeInTheDocument();
    expect(screen.queryByRole("progressbar")).toBeNull();
  });
});

// --------------------------------------------------------------------------- 三身份对照

describe("三身份对照（卡内 §3.6 的页面差异表）", () => {
  it("同一个创建弹窗在三种身份下内容确实不同", async () => {
    const collect = async (account: Account) => {
      stubRoutes({ "/api/auth/me": account, "/api/auth/tokens": [] });
      const user = userEvent.setup();
      const view = renderInRouter(<ApiKeysPage />);
      await screen.findByText("现有的 Key");
      await user.click(screen.getByRole("button", { name: "创建 Key" }));
      const dialog = await screen.findByRole("dialog");
      const text = dialog.textContent ?? "";
      view.unmount();
      vi.unstubAllGlobals();
      return text;
    };

    const asPublic = await collect(PUBLIC_ACCOUNT);
    const asBeta = await collect(BETA_ACCOUNT);
    const asAdmin = await collect(ADMIN_ACCOUNT);

    expect(new Set([asPublic, asBeta, asAdmin]).size).toBe(3);
    expect(asPublic).not.toContain("特权");
    expect(asBeta).toContain("拓荒者特权");
    expect(asAdmin).toContain("执炬者特权");
  });
});
