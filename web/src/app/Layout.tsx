/** 应用外壳：桌面左侧常驻导航，窄屏抽屉；保留路由、账户与登出契约。 */
import { useEffect, useRef, useState } from "react";
import { NavLink, Outlet, useNavigate } from "react-router-dom";

import { type Account, logout } from "../api/client";
import { Brand } from "../components/Brand";
import { Icon, type IconName } from "../components/Icon";

const NAV: { to: string; label: string; end: boolean; icon: IconName }[] = [
  { to: "/", label: "控制台", end: true, icon: "dashboard" },
  { to: "/projects", label: "项目", end: false, icon: "folder" },
  { to: "/connect", label: "接入指南", end: false, icon: "connect" },
  { to: "/keys", label: "API Key", end: false, icon: "key" },
  { to: "/history", label: "历史记录", end: false, icon: "history" },
  { to: "/settings", label: "设置", end: false, icon: "settings" },
];

export function Layout({
  account,
  onSignedOut,
}: {
  account: Account | null;
  onSignedOut: () => void;
}) {
  const navigate = useNavigate();
  const [drawerOpen, setDrawerOpen] = useState(false);
  const sidebarRef = useRef<HTMLElement>(null);
  const menuRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (!drawerOpen) return;
    const sidebar = sidebarRef.current;
    const menu = menuRef.current;
    const overflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    sidebar?.querySelector<HTMLElement>("a")?.focus();

    function onKey(event: KeyboardEvent) {
      if (event.key === "Escape") setDrawerOpen(false);
      if (event.key !== "Tab" || !sidebar) return;
      const items = [...sidebar.querySelectorAll<HTMLElement>("a, button:not(:disabled)")]
        .filter((item) => item.getClientRects().length > 0);
      const first = items[0];
      const last = items[items.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last?.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first?.focus();
      }
    }
    const media = window.matchMedia("(min-width: 768px)");
    function onResize() { if (media.matches) setDrawerOpen(false); }
    window.addEventListener("keydown", onKey);
    media.addEventListener("change", onResize);
    return () => {
      document.body.style.overflow = overflow;
      window.removeEventListener("keydown", onKey);
      media.removeEventListener("change", onResize);
      if (!media.matches) menu?.focus();
    };
  }, [drawerOpen]);

  async function onLogout() {
    try {
      await logout();
    } catch {
      // 登出失败也清本地状态。
    }
    onSignedOut();
    navigate("/login", { replace: true });
  }

  return (
    <div className="min-h-screen">
      <aside
        ref={sidebarRef}
        id="app-sidebar"
        data-testid="sidebar"
        className={`fixed inset-y-0 left-0 z-40 flex w-64 flex-col border-r-2 border-ink-line bg-paper-raised transition-transform duration-200 md:visible md:translate-x-0 ${
          drawerOpen ? "visible translate-x-0 shadow-xl" : "invisible -translate-x-full"
        }`}
      >
        <div className="flex items-start justify-between gap-2 border-b border-ink-line px-5 py-7">
          <NavLink to="/" className="min-w-0" onClick={() => setDrawerOpen(false)}>
            <Brand />
          </NavLink>
          <button type="button" aria-label="收起导航" onClick={() => setDrawerOpen(false)} className="p-1 text-ink-muted md:hidden">
            <Icon name="close" />
          </button>
        </div>

        <nav aria-label="主导航" className="flex-1 space-y-2 overflow-y-auto px-4 py-6 text-sm">
          {NAV.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.end}
              onClick={() => setDrawerOpen(false)}
              className={({ isActive }) => `sidebar-link ${isActive ? "bg-accent-seal/10 font-semibold text-accent-seal" : "text-ink-muted"}`}
            >
              <Icon name={item.icon} />
              <span className="flex-1">{item.label}</span>
              <Icon name="chevron" className="h-3.5 w-3.5 opacity-50" />
            </NavLink>
          ))}
          {account?.capabilities?.isAdmin && (
            <div className="mt-5 border-t border-dashed border-ink-line pt-5">
              <NavLink
                to="/admin"
                onClick={() => setDrawerOpen(false)}
                className={({ isActive }) => `sidebar-link ${isActive ? "bg-accent-seal/10 font-semibold text-accent-seal" : "text-ink-muted"}`}
              >
                <Icon name="shield" /><span className="flex-1">后台</span>
                <Icon name="chevron" className="h-3.5 w-3.5 opacity-50" />
              </NavLink>
            </div>
          )}
        </nav>

        {account && (
          <div className="border-t border-ink-line p-4 text-xs text-ink-muted">
            <div className="flex items-center gap-3 border border-ink-line bg-paper-card p-3">
              <span className="flex h-9 w-9 shrink-0 items-center justify-center border border-ink-line bg-paper-base text-accent-seal" aria-hidden="true"><Icon name="user" className="h-4 w-4" /></span>
              <div className="min-w-0 flex-1">
                <span className="block break-words text-sm font-semibold text-ink-primary">{account.name}</span>
                {account.isLocal ? <span>（本地）</span> : (
                  <span data-testid="sidebar-title" className="mt-1 block text-[11px]">
                    {account.earlyMemberNo === null ? account.title : `${account.title} #${String(account.earlyMemberNo).padStart(3, "0")}`}
                  </span>
                )}
              </div>
            </div>
            {!account.isLocal && (
              <button type="button" onClick={() => void onLogout()} className="ui-button mt-3 flex w-full items-center justify-center gap-2 border border-ink-line bg-paper-card px-3 py-2 text-ink-muted hover:bg-paper-base">
                <Icon name="logout" className="h-4 w-4" />登出
              </button>
            )}
          </div>
        )}
      </aside>

      <header className="sticky top-0 z-30 flex min-h-16 items-center gap-4 border-b-2 border-ink-line bg-paper-raised px-4 py-3 md:hidden">
        <button
          ref={menuRef}
          type="button"
          aria-label="导航菜单"
          aria-expanded={drawerOpen}
          aria-controls="app-sidebar"
          onClick={() => setDrawerOpen((open) => !open)}
          className="ui-button border border-ink-line bg-paper-card p-2 text-ink-primary"
        ><Icon name="menu" /></button>
        <Brand compact />
      </header>

      {drawerOpen && (
        <button type="button" aria-label="关闭导航抽屉" onClick={() => setDrawerOpen(false)} className="fixed inset-0 z-30 cursor-default bg-ink-primary/25 md:hidden" />
      )}

      <div className="min-w-0 md:pl-64">
        <main className="px-4 py-7 md:px-8 md:py-10 xl:px-12">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
