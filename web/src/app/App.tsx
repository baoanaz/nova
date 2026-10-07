/**
 * 路由与首屏门禁（TASK-071）。
 *
 * 首屏行为（用户 2026-09-13 指定"打开网页最先出现的应该是登入页面"）：
 *
 * | 部署状态 | 首屏 |
 * |---|---|
 * | 云端 + 有账户 + 未登录 | **登录页**（可在下方切到注册，若注册开启） |
 * | 云端 + 无账户 | **初始化账户**页（register 默认关闭时这是唯一入口） |
 * | 本地模式 | 直接进入（`GET /api/auth/me` 返回隐式账户；本地模式没有账户体系） |
 *
 * 判断顺序有讲究：先问 `/api/meta`（免鉴权、只回部署形态），再问 `/api/auth/me`
 * （判断登录态）。**不能**只靠 `/api/auth/me` 的 401 决定——那样在"全新部署、还没有任何账户"
 * 时会显示登录页，而用户根本没有账户可登。
 */

import { useCallback, useEffect, useState } from "react";
import { createBrowserRouter, Navigate, RouterProvider, useLocation } from "react-router-dom";

import { ApiError, type Account, getMe, getMeta } from "../api/client";
import { LoadingBlock } from "../components/ui";
import { AdminPage } from "../pages/AdminPage";
import { ApiKeysPage } from "../pages/ApiKeysPage";
import { ConnectPage } from "../pages/ConnectPage";
import { DashboardPage } from "../pages/DashboardPage";
import { HistoryPage } from "../pages/HistoryPage";
import { LoginPage } from "../pages/LoginPage";
import { ProjectsPage } from "../pages/ProjectsPage";
import { SettingsPage } from "../pages/SettingsPage";
import { Layout } from "./Layout";

type AuthState =
  | { kind: "loading" }
  | { kind: "anonymous"; needsBootstrap: boolean }
  | { kind: "signed-in"; account: Account };

export function App() {
  const [state, setState] = useState<AuthState>({ kind: "loading" });

  const probe = useCallback(async () => {
    try {
      const meta = await getMeta();
      if (!meta.authRequired) {
        // 本地模式：没有账户体系，直接取隐式账户进入。
        setState({ kind: "signed-in", account: await getMe() });
        return;
      }
      if (meta.needsBootstrap) {
        setState({
          kind: "anonymous",
          needsBootstrap: true,
        });
        return;
      }
      try {
        setState({ kind: "signed-in", account: await getMe() });
      } catch (err) {
        if (err instanceof ApiError && err.status === 401) {
          setState({
            kind: "anonymous",
            needsBootstrap: false,
          });
          return;
        }
        throw err;
      }
    } catch {
      // 服务不可达等：落到匿名态，登录页的请求会给出可操作提示。
      setState({ kind: "anonymous", needsBootstrap: false });
    }
  }, []);

  useEffect(() => {
    void probe();
  }, [probe]);

  if (state.kind === "loading") {
    return (
      <div className="min-h-screen">
        <LoadingBlock text="正在检查登录状态…" />
      </div>
    );
  }

  const router = buildRouter({
    account: state.kind === "signed-in" ? state.account : null,
    needsBootstrap: state.kind === "anonymous" ? state.needsBootstrap : false,
    onSignedIn: (account) => setState({ kind: "signed-in", account }),
    onSignedOut: () =>
      setState({ kind: "anonymous", needsBootstrap: false }),
  });

  return <RouterProvider router={router} />;
}

function buildRouter(options: {
  account: Account | null;
  needsBootstrap: boolean;
  onSignedIn: (account: Account) => void;
  onSignedOut: () => void;
}) {
  const { account, needsBootstrap, onSignedIn, onSignedOut } = options;

  /** 未登录 → 送去登录页（并记住原本想去的地址）。 */
  function Guard({ children }: { children: React.ReactNode }) {
    const location = useLocation();
    if (account === null) {
      return <Navigate to="/login" replace state={{ from: location.pathname }} />;
    }
    return <>{children}</>;
  }

  // basename 取 Vite 的 BASE_URL（TASK-092）：子路径部署时路由才能与资源前缀一致。
  return createBrowserRouter([
    {
      path: "/login",
      element:
        account !== null ? (
          <Navigate to="/" replace />
        ) : (
          <LoginPage onSignedIn={onSignedIn} />
        ),
    },
    {
      path: "/",
      element: account === null ? <LoginPage onSignedIn={onSignedIn} /> : <Layout account={account} onSignedOut={onSignedOut} />,
      children: [
        { index: true, element: <Guard><DashboardPage /></Guard> },
        // TASK-100：项目页与控制台同级（用户 2026-09-14 要求）。
        { path: "projects", element: <Guard><ProjectsPage /></Guard> },
        { path: "keys", element: <Guard><ApiKeysPage /></Guard> },
        { path: "history", element: <Guard><HistoryPage /></Guard> },
        { path: "connect", element: <Guard><ConnectPage /></Guard> },
        // TASK-110：账户信息已**合并进控制台**（用户 2026-09-15 要求），因此 /account
        // 保留为跳转（外部链接与文档里出现过，不能让它 404）。
        { path: "account", element: <Navigate to="/" replace /> },
        {
          path: "admin",
          // **双重门禁**：前端只是"不给入口"，真正的门禁是后端的 require_admin（每个
          // /api/admin/* 都会 403）。这里多一层是为了不把无权限用户送到一个只会报错的页面。
          // 用可选链：能力位是 TASK-110 新增的，旧服务端/未升级的部署可能还没有它——
          // 那种情况下应该回控制台，而不是把整个应用崩在一句 undefined 上。
          element: (
            <Guard>
              {account?.capabilities?.isAdmin ? <AdminPage /> : <Navigate to="/" replace />}
            </Guard>
          ),
        },
        // 旧路径保留为跳转（外部链接与文档里出现过）。
        { path: "setup", element: <Navigate to="/login" replace /> },
        { path: "register", element: <Navigate to="/login" replace /> },
        { path: "tokens", element: <Navigate to="/keys" replace /> },
        { path: "usage", element: <Navigate to="/history" replace /> },
        { path: "settings", element: <Guard><SettingsPage /></Guard> },
        { path: "login", element: <Navigate to={needsBootstrap ? "/login" : "/"} replace /> },
        { path: "*", element: <Navigate to={account === null ? "/login" : "/"} replace /> },
      ],
    },
  ], { basename: import.meta.env.BASE_URL });
}
