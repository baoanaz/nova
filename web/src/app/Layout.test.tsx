/**
 * 应用外壳导航（TASK-086 §1；TASK-088 §F 追加“设置”；TASK-100 §需求4 追加“项目”）。
 *
 * 守两件事（本卡真正会退化、且用户直接看得见的地方）：
 * 1. **导航顺序 = 控制台 → 项目 → 接入指南 → API Key → 历史记录 → 设置**
 *    （TASK-100 把「项目」插在控制台之后——它与控制台同级，都是“看数据”的页；
 *    其余项的顺序由 TASK-086/088 定下，不得因新增而变动）；
 * 2. **`to` 是路由契约**——顺序变了，路径一个都不能变（外部链接与文档都指向它们）。
 *
 * 另外顺手钉住 §4 的一个易破点：侧边栏必须**不透明**，
 * 否则加了全局背景纹理后内容滚动会透过导航文字。
 *
 * --- TASK-098（复古主题 + 侧边栏改版）的同步说明 ---
 *
 * 改版把顶栏换成侧边栏，并把 `slate-*` / `bg-white` 换成 `paper` / `ink` / `accent` token。
 * 本文件因此同步更新样式断言——**但没有删掉任何一条**：
 *
 * | 原断言 | 现断言 | 守护的东西（不变） |
 * |---|---|---|
 * | `expect(…控制台).not.toContain("bg-slate-900")` | `…not.toContain("bg-accent-seal")` | 非 active 项不高亮（`end` 生效） |
 * | `expect(…接入指南).toContain("bg-slate-900")` | `…toContain("bg-accent-seal")` | active 态可辨识 |
 * | `getByRole("banner")` 的 `bg-white` | `getByTestId("sidebar")` 的 `bg-paper-raised` | 导航底色不透明 |
 *
 * 为何最后一条换了元素与 role：侧边栏是 `<aside>`，其隐式 role 为 `complementary` 而不是
 * `banner`（`banner` 只对应 `<header>` 且要求不在 section 内）。改版后仍存在 `<header>`，
 * 但它只是**窄屏顶栏**（`md:hidden`），不再是承载导航的常驻面——用 `banner` 断言会测错对象。
 * 因此改为断言真正承载导航的侧边栏，并**同时**断言窄屏顶栏的底色，覆盖面不缩小。
 */

import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";

import { type Account } from "../api/client";
import { Layout } from "./Layout";

/** 一个**普通（公测）用户**的完整账户（TASK-110 后 Account 带身份与能力位）。 */
const PUBLIC_ACCOUNT: Account = {
  userId: "u1",
  name: "owner",
  createdAt: 1789300000,
  isLocal: false,
  via: "session",
  role: "public" as const,
  title: "旅人",
  earlyMemberNo: null,
  userNo: 12,
  capabilities: { canCustomKey: false, quotaBytes: 500 * 1024 * 1024, earlyMemberNo: null, isAdmin: false },
};

/** 内测用户的账户（带编号；用于徽章与"页面确实不同"的断言）。 */
const BETA_ACCOUNT: Account = {
  ...PUBLIC_ACCOUNT,
  role: "beta",
  title: "拓荒者",
  earlyMemberNo: 27,
  capabilities: {
    canCustomKey: true,
    quotaBytes: 1024 * 1024 * 1024,
    earlyMemberNo: 27,
    isAdmin: false,
  },
};

/** 管理员的账户（后台入口的依据）。 */
const ADMIN_ACCOUNT: Account = {
  ...PUBLIC_ACCOUNT,
  role: "admin",
  title: "执炬者",
  capabilities: {
    canCustomKey: true,
    quotaBytes: 5 * 1024 * 1024 * 1024,
    earlyMemberNo: null,
    isAdmin: true,
  },
};

function renderLayout(initialPath = "/", account: Account | null = null) {
  return render(
    <MemoryRouter initialEntries={[initialPath]}>
      <Layout account={account} onSignedOut={() => {}} />
    </MemoryRouter>,
  );
}

/**
 * 只取主导航里的链接（排除侧边栏顶部的 "nova" 品牌链接）。
 *
 * TASK-098：主导航从 `<header><nav>` 移到 `<aside>`，但仍带 `aria-label="主导航"`，
 * 因此仍用 `getByRole("navigation")` 定位——语义没变，测试不必知道布局细节。
 */
function navLinks() {
  return screen.getByRole("navigation", { name: "主导航" }).querySelectorAll("a");
}

/**
 * 导航标签（TASK-110 后普通用户为 7 项；管理员多一项「后台」）。
 *
 * 把期望写成常量而不是散在各用例：导航顺序是**用户指定的信息架构**（TASK-086/100），
 * 新增一项时必须在这里显式改一次（而不是让某条断言“恰好也过了”）。
 */
const NAV_LABELS = [
  "控制台",
  "项目",
  "接入指南",
  "API Key",
  "历史记录",
  "设置",
];
const NAV_HREFS = [
  "/",
  "/projects",
  "/connect",
  "/keys",
  "/history",
  "/settings",
];

/** 取某个导航项的高亮 class；找不到就抛出（而不是断言非空）。 */
function navLinkClass(label: string): string {
  const link = [...navLinks()].find((item) => item.textContent === label);
  if (!link) throw new Error(`主导航里没有「${label}」`);
  return link.className;
}

describe("主导航（TASK-086 §1 / TASK-088 §F / TASK-100 §需求4）", () => {
  it("顺序为 控制台 → 项目 → 接入指南 → API Key → 历史记录 → 设置", () => {
    renderLayout("/", PUBLIC_ACCOUNT);

    expect([...navLinks()].map((link) => link.textContent)).toEqual(NAV_LABELS);
  });

  it("插入项目项后，其余各项的路径一个都没变", () => {
    renderLayout("/", PUBLIC_ACCOUNT);

    expect([...navLinks()].map((link) => link.getAttribute("href"))).toEqual(NAV_HREFS);
  });

  it("控制台仍指向 / 且 end 生效：/connect 时它不高亮", () => {
    renderLayout("/connect");

    // 若 "/" 丢了 `end`，它会在每个子路径上都保持高亮。
    // TASK-098：active 底色由 `bg-slate-900` 换成 `accent.seal`（TASK-100 后为深赭褐）。
    expect(navLinkClass("控制台")).not.toContain("bg-accent-seal");
    expect(navLinkClass("接入指南")).toContain("bg-accent-seal");
  });

  it("侧边栏保持不透明底色（TASK-086 §4：不能透过导航文字）", () => {
    renderLayout();

    // TASK-098：导航从 `<header role=banner>` 移到 `<aside>`（隐式 role=complementary），
    // 故改用 testid 定位。守护的东西不变：导航底色必须是不透明色，不能是透明/半透明。
    expect(screen.getByTestId("sidebar").className).toContain("bg-paper-raised");
  });

  it("窄屏顶栏（汉堡按钮所在）同样不透明", () => {
    renderLayout();

    // 窄屏下侧边栏是抽屉，顶栏成为唯一常驻的滚动遮挡面——它也必须不透明。
    const header = screen.getByRole("banner");
    expect(header.className).toContain("bg-paper-raised");
  });

  it("账户名与登出仍在侧边栏底部（TASK-098 §C）", () => {
    render(
      <MemoryRouter initialEntries={["/"]}>
        <Layout account={PUBLIC_ACCOUNT} onSignedOut={() => {}} />
      </MemoryRouter>,
    );

    const sidebar = screen.getByTestId("sidebar");
    expect(sidebar.textContent).toContain("owner");
    expect(sidebar.textContent).toContain("登出");
  });
});

describe("身份分级（TASK-110）", () => {
  it("普通（公测）用户看不到「后台」入口", () => {
    renderLayout("/", PUBLIC_ACCOUNT);
    expect([...navLinks()].map((link) => link.textContent)).not.toContain("后台");
  });

  it("内测用户也看不到「后台」入口（它不是管理员）", () => {
    renderLayout("/", BETA_ACCOUNT);
    expect([...navLinks()].map((link) => link.textContent)).not.toContain("后台");
  });

  it("管理员能看到「后台」入口且指向 /admin", () => {
    renderLayout("/", ADMIN_ACCOUNT);
    const admin = [...navLinks()].find((link) => link.textContent === "后台");
    expect(admin).toBeDefined();
    expect(admin?.getAttribute("href")).toBe("/admin");
  });

  it("内测用户的编号展示为 拓荒者 #027（三位补零）", () => {
    renderLayout("/", BETA_ACCOUNT);
    expect(screen.getByTestId("sidebar-title").textContent).toBe("拓荒者 #027");
  });

  it("无编号的用户只展示头衔", () => {
    renderLayout("/", PUBLIC_ACCOUNT);
    expect(screen.getByTestId("sidebar-title").textContent).toBe("旅人");
  });

  it("本地模式不展示身份徽章（它没有身份体系）", () => {
    renderLayout("/", { ...PUBLIC_ACCOUNT, isLocal: true });
    expect(screen.queryByTestId("sidebar-title")).toBeNull();
  });
});
