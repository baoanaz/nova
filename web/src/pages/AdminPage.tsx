/**
 * 管理员后台（TASK-110 §3.6 / §1.6；2026-09-15 按用户要求改版）。
 *
 * 四个标签（原五个，"调用统计"与"项目"已合并为"信息查询"）：
 *
 * | 标签 | 内容 |
 * |---|---|
 * | 用户 | 改身份 / 额度 / 封禁 |
 * | 邀请码 | 创建 / 失效 / 使用记录 |
 * | **信息查询** | 项目表（按占用降序、可按用户筛选、可删除）+ 该范围的调用统计 |
 * | 系统状态 | 服务 + Embedding/LLM + **VPS 内存** |
 *
 * 用户 2026-09-15 的原话要求（逐条落在下面的实现里）：
 *
 * - "后台项目这一栏可以选择全部人员，进行内存从大到小排序，也可以筛选某一个用户的全部项目" →
 *   ``信息查询`` 上方的用户下拉 + 后端排序（**排序在后端做**：占用是目录递归求和的结果，
 *   前端排序意味着全量传一遍再排，而用户看到的顺序必须与总量一致）；
 * - "并且可以拥有删除权限，删除后，用户存储的索引就没了" → 每行「删除」+ 二次确认；
 * - "归属这边显示名称吧，一串编号我也不知道什么意思" → 显示 ``ownerName``（未认领如实显示）；
 * - "再来一个用户查询，可以下拉查询某个用户的数据" → 同一个下拉同时筛选项目与统计；
 * - "系统状态，加一个 vps 当前内存占用情况" → ``host`` 一节。
 *
 * 两条纪律：
 * 1. **入口由后端决定**：本页只在 ``account.capabilities.isAdmin`` 为真时可达；
 * 2. **不重算后端已算好的数**：占用用 ``usedText`` 与 ``formatBytes``，额度用
 *    ``effectiveQuotaBytes``——自己换算单位会与账户页在舍入上漂移。
 */

import { useCallback, useEffect, useState } from "react";

import {
  type AdminOwnerOption,
  type AdminProject,
  type AdminStats,
  type AdminUser,
  type Health,
  type HostMemory,
  type Invite,
  createAdminInvite,
  deleteAdminProject,
  getAdminStats,
  getAdminSystem,
  listAdminInvites,
  listAdminProjects,
  listAdminUsers,
  patchAdminUser,
  revokeAdminInvite,
} from "../api/client";
import { EmptyState, ErrorBlock, LoadingBlock, Modal, Page } from "../components/ui";
import { formatBytes, formatTime } from "./DashboardPage";

type Tab = "users" | "invites" | "insights" | "system";

const TABS: [Tab, string][] = [
  ["users", "用户"],
  ["invites", "邀请码"],
  ["insights", "信息查询"],
  ["system", "系统状态"],
];

const ROLE_OPTIONS: [string, string][] = [
  ["public", "公测（旅人）"],
  ["beta", "内测（拓荒者）"],
  ["admin", "管理员（执炬者）"],
];

export function AdminPage() {
  const [tab, setTab] = useState<Tab>("users");
  return (
    <Page>
      <div>
        <h1 className="text-lg font-semibold">管理后台</h1>
        <p className="mt-1 text-sm text-ink-muted">运营与排查工具（仅管理员可见）。</p>
      </div>

      <div className="flex flex-wrap gap-1 border-b border-ink-line pb-2 text-sm">
        {TABS.map(([value, label]) => (
          <button
            key={value}
            type="button"
            onClick={() => setTab(value)}
            className={`rounded px-3 py-1.5 ${
              tab === value
                ? "bg-accent-seal text-accent-contrast"
                : "text-ink-muted hover:bg-paper-base hover:text-ink-primary"
            }`}
          >
            {label}
          </button>
        ))}
      </div>

      {tab === "users" && <UsersTab />}
      {tab === "invites" && <InvitesTab />}
      {tab === "insights" && <InsightsTab />}
      {tab === "system" && <SystemTab />}
    </Page>
  );
}

// --------------------------------------------------------------------------- 用户

function UsersTab() {
  const [users, setUsers] = useState<AdminUser[] | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  //: 正在编辑配额的用户（弹窗）；``null`` = 没开。
  const [editing, setEditing] = useState<AdminUser | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      setUsers((await listAdminUsers()).users);
    } catch (err) {
      setUsers(null);
      setError(err);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function patch(userId: string, body: Parameters<typeof patchAdminUser>[1]) {
    setBusy(true);
    setError(null);
    try {
      await patchAdminUser(userId, body);
      await load();
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  }

  if (error !== null) return <ErrorBlock error={error} />;
  if (users === null) return <LoadingBlock />;

  return (
    <>
      <section className="rounded-lg border border-ink-line bg-paper-card shadow-sm">
        {users.length === 0 ? (
          <EmptyState title="还没有用户" />
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full border-collapse text-sm">
              <thead>
                <tr className="text-left text-xs text-ink-muted">
                  <Th>账户</Th>
                  <Th>身份</Th>
                  <Th>项目</Th>
                  <Th>索引占用</Th>
                  <Th>额度</Th>
                  <Th>检索次数</Th>
                  <Th>最后活跃</Th>
                  <Th>操作</Th>
                </tr>
              </thead>
              <tbody>
                {users.map((user) => (
                  <tr key={user.userId} className="border-t border-ink-line/60">
                    <td className="px-4 py-2">
                      <span className="text-ink-primary">{user.name}</span>
                      {user.bannedAt !== null && (
                        <span className="ml-2 rounded bg-error-soft px-1.5 py-0.5 text-xs text-error-text">
                          已封禁
                        </span>
                      )}
                      <div className="text-xs text-ink-muted">
                        {user.userNo != null && (
                          <span className="mr-2 font-mono">
                            ID #{String(user.userNo).padStart(3, "0")}
                          </span>
                        )}
                        注册于 {formatTime(user.createdAt)}
                      </div>
                    </td>
                    <td className="px-4 py-2">
                      <select
                        value={user.role}
                        disabled={busy}
                        onChange={(event) =>
                          void patch(user.userId, { role: event.target.value })
                        }
                        className="rounded border border-ink-line bg-paper-card px-2 py-1 text-xs"
                      >
                        {ROLE_OPTIONS.map(([value, label]) => (
                          <option key={value} value={value}>
                            {label}
                          </option>
                        ))}
                      </select>
                      {user.earlyMemberNo !== null && (
                        <div className="mt-1 font-mono text-xs text-ink-muted">
                          #{String(user.earlyMemberNo).padStart(3, "0")}
                        </div>
                      )}
                    </td>
                    <td className="px-4 py-2 text-xs">{user.projectCount}</td>
                    <td className="px-4 py-2 text-xs">{user.usedText}</td>
                    <td className="px-4 py-2">
                      <button
                        type="button"
                        onClick={() => setEditing(user)}
                        className="rounded border border-ink-line px-2 py-0.5 text-xs text-ink-primary hover:bg-paper-base"
                        title="点击修改该用户的索引空间上限"
                      >
                        {formatQuota(user.effectiveQuotaBytes)}
                        {user.quotaBytes !== null && (
                          <span className="ml-1 text-[11px] text-warning-text">人工</span>
                        )}
                      </button>
                    </td>
                    <td className="px-4 py-2 text-xs">{user.queryCount}</td>
                    <td className="px-4 py-2 text-xs text-ink-muted">
                      {user.lastSeenAt === null ? "—" : formatTime(user.lastSeenAt)}
                    </td>
                    <td className="px-4 py-2">
                      <button
                        type="button"
                        disabled={busy}
                        onClick={() =>
                          void patch(user.userId, { banned: user.bannedAt === null })
                        }
                        className={`rounded border px-2 py-1 text-xs ${
                          user.bannedAt === null
                            ? "border-error-line text-error-text hover:bg-error-soft"
                            : "border-success-line text-success-text hover:bg-success-soft"
                        }`}
                      >
                        {user.bannedAt === null ? "封禁" : "恢复"}
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {editing !== null && (
        <QuotaDialog
          user={editing}
          busy={busy}
          onClose={() => setEditing(null)}
          onSave={async (value) => {
            await patch(editing.userId, { quotaBytes: value });
            setEditing(null);
          }}
        />
      )}
    </>
  );
}

/**
 * 改配额弹窗（TASK-110 §1.6“修改配额”）。
 *
 * 三种输入：留空 = 恢复按角色默认；``0`` = 不限；正数 = 指定字节数。
 * 为什么不自制滑块或 MB 输入框：额度是**运维动作**而不是日常操作，精确字节数比
 * “拖一个滑块猜”更可预期（而且拖拉滑块的粒度会随口子变化而漂移）。
 */
function QuotaDialog({
  user,
  busy,
  onClose,
  onSave,
}: {
  user: AdminUser;
  busy: boolean;
  onClose: () => void;
  onSave: (value: number | null) => Promise<void>;
}) {
  const [value, setValue] = useState(
    user.quotaBytes === null ? "" : String(user.quotaBytes),
  );
  const [error, setError] = useState<unknown>(null);

  const trimmed = value.trim();
  const resolved: number | null = trimmed === "" ? null : Number(trimmed);
  const invalid = resolved !== null && (!Number.isInteger(resolved) || resolved < 0);

  return (
    <Modal title={`修改额度：${user.name}`} onClose={busy ? undefined : onClose}>
      <div className="space-y-3 text-sm">
        <p className="text-xs text-ink-muted">
          当前生效 {formatQuota(user.effectiveQuotaBytes)}
          {user.quotaBytes === null ? "（按角色默认）" : "（人工指定）"}。
        </p>
        <label className="block">
          <span className="mb-1 block text-xs text-ink-muted">
            字节数（留空 = 恢复按角色默认；0 = 不限）
          </span>
          <input
            value={value}
            onChange={(event) => setValue(event.target.value)}
            placeholder="留空"
            inputMode="numeric"
            className="w-full rounded border border-ink-line bg-paper-card px-3 py-1.5 font-mono text-sm text-ink-primary"
          />
        </label>
        <p className="text-[11px] text-ink-muted">
          常用值：1 GiB = {String(1024 ** 3)} ｜ 500 MiB = {String(500 * 1024 ** 2)}
        </p>
        {invalid && <p className="text-xs text-error-text">请输入非负整数（或留空）。</p>}
        {error !== null && <ErrorBlock error={error} />}
        <div className="flex justify-end gap-2 pt-1">
          <button
            type="button"
            onClick={onClose}
            disabled={busy}
            className="rounded border border-ink-line px-3 py-1.5 text-sm text-ink-primary hover:bg-paper-base disabled:opacity-50"
          >
            取消
          </button>
          <button
            type="button"
            disabled={busy || invalid}
            onClick={() => {
              setError(null);
              void onSave(resolved).catch(setError);
            }}
            className="rounded bg-accent-seal px-3 py-1.5 text-sm font-medium text-accent-contrast hover:opacity-90 disabled:opacity-40"
          >
            {busy ? "保存中…" : "保存"}
          </button>
        </div>
      </div>
    </Modal>
  );
}

// --------------------------------------------------------------------------- 邀请码

function InvitesTab() {
  const [invites, setInvites] = useState<Invite[] | null>(null);
  const [kinds, setKinds] = useState<Record<string, string>>({});
  const [error, setError] = useState<unknown>(null);
  const [kind, setKind] = useState("C");
  const [maxUses, setMaxUses] = useState(1);
  const [expiresInDays, setExpiresInDays] = useState<string>("");
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    setError(null);
    try {
      const body = await listAdminInvites();
      setInvites(body.invites);
      setKinds(body.kinds);
    } catch (err) {
      setInvites(null);
      setError(err);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function onCreate(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await createAdminInvite({
        kind,
        maxUses,
        expiresInDays: expiresInDays.trim() === "" ? null : Number(expiresInDays),
        code: code.trim().toUpperCase(),
      });
      setCode("");
      await load();
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  }

  async function onRevoke(target: string) {
    if (!window.confirm(`确认失效邀请码 ${target}？`)) return;
    try {
      await revokeAdminInvite(target);
      await load();
    } catch (err) {
      setError(err);
    }
  }

  return (
    <div className="space-y-4">
      {error !== null && <ErrorBlock error={error} />}

      <form
        onSubmit={onCreate}
        className="flex flex-wrap items-end gap-3 rounded-lg border border-ink-line bg-paper-card p-4 shadow-sm"
      >
        <label className="text-sm">
          <span className="mb-1 block text-xs text-ink-muted">类型</span>
          <select
            value={kind}
            onChange={(event) => setKind(event.target.value)}
            className="rounded border border-ink-line bg-paper-card px-3 py-1.5 text-sm"
          >
            <option value="A">A · 管理员</option>
            <option value="B">B · 内测（拓荒者）</option>
            <option value="C">C · 公测（旅人）</option>
          </select>
        </label>
        <label className="text-sm">
          <span className="mb-1 block text-xs text-ink-muted">可用次数</span>
          <input
            type="number"
            min={1}
            max={10000}
            value={maxUses}
            onChange={(event) => setMaxUses(Number(event.target.value))}
            className="w-24 rounded border border-ink-line bg-paper-card px-3 py-1.5 text-sm"
          />
        </label>
        <label className="text-sm">
          <span className="mb-1 block text-xs text-ink-muted">有效期（天，空 = 永久）</span>
          <input
            type="number"
            min={1}
            max={3650}
            value={expiresInDays}
            onChange={(event) => setExpiresInDays(event.target.value)}
            placeholder="永久"
            className="w-32 rounded border border-ink-line bg-paper-card px-3 py-1.5 text-sm"
          />
        </label>
        <label className="text-sm">
          <span className="mb-1 block text-xs text-ink-muted">指定码面（空 = 随机）</span>
          <input
            value={code}
            onChange={(event) => setCode(event.target.value.toUpperCase())}
            maxLength={6}
            placeholder="如 B7K2M9"
            className="w-32 rounded border border-ink-line bg-paper-card px-3 py-1.5 font-mono text-sm tracking-widest"
          />
        </label>
        <button
          type="submit"
          disabled={busy}
          className="rounded bg-accent-seal px-4 py-1.5 text-sm text-accent-contrast disabled:opacity-40"
        >
          {busy ? "创建中…" : "创建邀请码"}
        </button>
      </form>

      <section className="rounded-lg border border-ink-line bg-paper-card shadow-sm">
        {invites === null ? (
          <LoadingBlock />
        ) : invites.length === 0 ? (
          <EmptyState title="还没有邀请码" hint="用上方表单创建第一张。" />
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full border-collapse text-sm">
              <thead>
                <tr className="text-left text-xs text-ink-muted">
                  <Th>码</Th>
                  <Th>类型</Th>
                  <Th>使用</Th>
                  <Th>有效期</Th>
                  <Th>使用记录</Th>
                  <Th>状态</Th>
                  <Th>操作</Th>
                </tr>
              </thead>
              <tbody>
                {invites.map((invite) => (
                  <tr key={invite.code} className="border-t border-ink-line/60">
                    <td className="px-4 py-2 font-mono text-xs tracking-widest">
                      {invite.code}
                    </td>
                    <td className="px-4 py-2 text-xs">
                      {invite.kind} · {kinds[invite.kind] ?? "—"}
                    </td>
                    <td className="px-4 py-2 text-xs">
                      {invite.usedCount} / {invite.maxUses}
                    </td>
                    <td className="px-4 py-2 text-xs text-ink-muted">
                      {invite.expiresAt === null ? "永久" : formatTime(invite.expiresAt)}
                    </td>
                    <td className="px-4 py-2 text-xs text-ink-muted">
                      {(invite.uses ?? []).length === 0
                        ? "—"
                        : (invite.uses ?? [])
                            .map(
                              (use) =>
                                `${use.userName ?? use.userId.slice(0, 6)} @ ${formatTime(use.usedAt)}`,
                            )
                            .join("；")}
                    </td>
                    <td className="px-4 py-2 text-xs">
                      {invite.revokedAt !== null ? (
                        <span className="text-error-text">已失效</span>
                      ) : (
                        <span className="text-success-text">有效</span>
                      )}
                    </td>
                    <td className="px-4 py-2">
                      {invite.revokedAt === null && (
                        <button
                          type="button"
                          onClick={() => void onRevoke(invite.code)}
                          className="rounded border border-error-line px-2 py-1 text-xs text-error-text hover:bg-error-soft"
                        >
                          失效
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}

// --------------------------------------------------------------------------- 信息查询

/**
 * 信息查询（用户 2026-09-15 要求合并"调用统计"与"项目"）。
 *
 * 顶部一个用户下拉**同时**作用于项目表与统计卡：用户排查某个人时，
 * 两处范围必须一致（否则"他占了 30 MB 但统计里只有 2 次检索"会让人以为数据丢了）。
 */
function InsightsTab() {
  const [userId, setUserId] = useState<string>("");
  const [projects, setProjects] = useState<AdminProject[] | null>(null);
  const [totalBytes, setTotalBytes] = useState(0);
  const [owners, setOwners] = useState<AdminOwnerOption[]>([]);
  const [stats, setStats] = useState<AdminStats | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    setError(null);
    try {
      const scope = userId || null;
      const [listed, usage] = await Promise.all([
        listAdminProjects(scope),
        getAdminStats(30, scope),
      ]);
      setProjects(listed.projects);
      setTotalBytes(listed.totalBytes);
      setOwners(listed.owners);
      setStats(usage);
    } catch (err) {
      setProjects(null);
      setStats(null);
      setError(err);
    }
  }, [userId]);

  useEffect(() => {
    void load();
  }, [load]);

  async function onDelete(project: AdminProject) {
    const label = project.displayName || project.projectId;
    const confirmed = window.confirm(
      `确认删除项目「${label}」？\n\n` +
        `将删除该项目在服务端的全部索引数据（约 ${formatBytes(project.diskBytes)}），` +
        `用户的源码文件不受影响但需要重新索引。\n\n此操作不可撤销。`,
    );
    if (!confirmed) return;
    setBusy(true);
    setError(null);
    try {
      await deleteAdminProject(project.projectId);
      await load();
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3 rounded-lg border border-ink-line bg-paper-card px-4 py-3 shadow-sm">
        <label className="flex items-center gap-2 text-sm">
          <span className="text-xs text-ink-muted">查询范围</span>
          <select
            value={userId}
            onChange={(event) => setUserId(event.target.value)}
            className="rounded border border-ink-line bg-paper-card px-3 py-1.5 text-sm"
          >
            <option value="">全部人员</option>
            {owners.map((owner) => (
              <option key={owner.userId} value={owner.userId}>
                {owner.name}
                {owner.userNo != null ? ` #${String(owner.userNo).padStart(3, "0")}` : ""}
              </option>
            ))}
          </select>
        </label>
        <span className="text-xs text-ink-muted">
          项目按占用从大到小排序；共 {projects?.length ?? 0} 个 /{" "}
          {formatBytes(totalBytes)}
        </span>
      </div>

      {error !== null && <ErrorBlock error={error} />}

      {stats !== null && (
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
          <Panel title={`检索 / 提问（近 ${stats.days} 天）`}>
            <Row label="总调用" value={`${stats.search.total} 次`} />
            <Row label="有答案" value={String(stats.search.succeeded)} />
            <Row label="证据不足" value={String(stats.search.insufficient)} />
            <Row label="降级失败" value={String(stats.search.failed)} />
            <Row
              label="错误率"
              value={stats.errorRate === null ? "—" : `${(stats.errorRate * 100).toFixed(1)}%`}
            />
            <Row label="Token 用量" value={String(stats.tokens)} />
            <Row
              label="平均耗时"
              value={
                stats.search.avgLatencyMs === null ? "—" : `${stats.search.avgLatencyMs} ms`
              }
            />
            <Row
              label="P95 耗时"
              value={
                stats.search.p95LatencyMs === null ? "—" : `${stats.search.p95LatencyMs} ms`
              }
            />
          </Panel>

          <Panel title="索引">
            <Row label="项目数" value={String(stats.projectCount)} />
            <Row label="总次数" value={String(stats.index.total)} />
            <Row label="成功" value={String(stats.index.succeeded)} />
            <Row label="失败" value={String(stats.index.failed)} />
            <Row
              label="平均耗时"
              value={stats.index.avgDurationMs === null ? "—" : `${stats.index.avgDurationMs} ms`}
            />
          </Panel>
        </div>
      )}

      <section className="rounded-lg border border-ink-line bg-paper-card shadow-sm">
        {projects === null ? (
          <LoadingBlock />
        ) : projects.length === 0 ? (
          <EmptyState
            title={userId ? "该用户还没有项目" : "还没有项目"}
            hint="用户第一次同步或绑定仓库后，这里会出现记录。"
          />
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full border-collapse text-sm">
              <thead>
                <tr className="text-left text-xs text-ink-muted">
                  <Th>项目</Th>
                  <Th>归属</Th>
                  <Th>占用</Th>
                  <Th>索引历史</Th>
                  <Th>最近状态</Th>
                  <Th>失败原因 / 跳过</Th>
                  <Th>操作</Th>
                </tr>
              </thead>
              <tbody>
                {projects.map((project) => (
                  <tr key={project.projectId} className="border-t border-ink-line/60">
                    <td className="px-4 py-2">
                      <span className="text-ink-primary">
                        {project.displayName || project.projectId}
                      </span>
                      <div className="font-mono text-[11px] text-ink-muted">
                        {project.projectId}
                      </div>
                      {project.attachedRoot && (
                        <div className="font-mono text-[11px] text-ink-muted">
                          {project.attachedRoot}
                        </div>
                      )}
                    </td>
                    <td className="px-4 py-2 text-xs">
                      {project.ownerName ?? (
                        <span className="text-ink-muted">未认领</span>
                      )}
                      {project.ownerNo != null && (
                        <div className="font-mono text-[11px] text-ink-muted">
                          #{String(project.ownerNo).padStart(3, "0")}
                        </div>
                      )}
                    </td>
                    <td className="px-4 py-2 text-xs">{formatBytes(project.diskBytes)}</td>
                    <td className="px-4 py-2 text-xs">
                      共 {project.history.total} · 成功 {project.history.succeeded} · 失败{" "}
                      {project.history.failed}
                    </td>
                    <td className="px-4 py-2 text-xs">
                      {project.history.lastState ?? "—"}
                      <div className="text-[11px] text-ink-muted">
                        {project.history.lastRunAt === null
                          ? "—"
                          : formatTime(project.history.lastRunAt)}
                      </div>
                    </td>
                    <td className="max-w-xs px-4 py-2 text-xs">
                      {project.lastError ? (
                        <span className="break-all text-error-text">{project.lastError}</span>
                      ) : (
                        <span className="text-ink-muted">—</span>
                      )}
                      {project.lastSkipped !== null && project.lastSkipped > 0 && (
                        <div className="text-[11px] text-ink-muted">
                          上次跳过 {project.lastSkipped} 个文件
                        </div>
                      )}
                    </td>
                    <td className="px-4 py-2">
                      <button
                        type="button"
                        disabled={busy}
                        onClick={() => void onDelete(project)}
                        className="rounded border border-error-line px-2 py-1 text-xs text-error-text hover:bg-error-soft disabled:opacity-40"
                      >
                        删除
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}

// --------------------------------------------------------------------------- 系统

function SystemTab() {
  const [health, setHealth] = useState<(Health & { host?: HostMemory }) | null>(null);
  const [error, setError] = useState<unknown>(null);

  useEffect(() => {
    void (async () => {
      try {
        setHealth(await getAdminSystem());
      } catch (err) {
        setError(err);
      }
    })();
  }, []);

  if (error !== null) return <ErrorBlock error={error} />;
  if (health === null) return <LoadingBlock />;

  return (
    <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
      <Panel title="服务">
        <Row label="状态" value={health.status} />
        <Row label="版本" value={health.version} />
        <Row label="部署形态" value={health.localMode ? "本地单用户" : "云端"} />
        <Row label="鉴权" value={health.auth} />
        <Row label="数据根" value={<span className="font-mono text-xs">{health.dataRoot}</span>} />
      </Panel>

      <Panel title="VPS 内存">
        <HostMemoryPanel host={health.host} />
      </Panel>

      <Panel title="Embedding / LLM">
        <Row label="core 可导入" value={health.core.importable ? "是" : "否"} />
        {health.core.ok !== undefined && (
          <Row label="provider 探测" value={health.core.ok ? "正常" : "异常"} />
        )}
        {health.core.modelId && <Row label="模型" value={health.core.modelId} />}
        {health.core.dim !== undefined && <Row label="维度" value={String(health.core.dim)} />}
        {health.core.reason && (
          <Row
            label="原因"
            value={<span className="break-all text-error-text">{health.core.reason}</span>}
          />
        )}
      </Panel>
    </div>
  );
}

/**
 * VPS 内存（用户 2026-09-15 要求）。
 *
 * 读不到时（非 Linux）**如实说明原因**，而不是显示 0——"没读到"与"没占用"是两件事
 * （与全库的诚实性口径一致：未测量不填 0）。
 */
function HostMemoryPanel({ host }: { host?: HostMemory }) {
  if (host === undefined) {
    return (
      <Row
        label="内存"
        value={<span className="text-ink-muted">后端未提供（服务版本较旧）</span>}
      />
    );
  }
  if (host.reason !== null || host.totalBytes === null) {
    return <Row label="内存" value={<span className="text-ink-muted">{host.reason ?? "—"}</span>} />;
  }
  const percent = host.usedRatio === null ? null : host.usedRatio * 100;
  return (
    <>
      <Row
        label="已用 / 总量"
        value={
          <span>
            {formatBytes(host.usedBytes)}
            <span className="text-ink-muted"> / {formatBytes(host.totalBytes)}</span>
          </span>
        }
      />
      <Row label="可用" value={formatBytes(host.availableBytes)} />
      <Row
        label="使用率"
        value={percent === null ? "—" : `${percent.toFixed(1)}%`}
      />
      {percent !== null && (
        <div className="py-2">
          <div
            className="h-2 w-full overflow-hidden rounded bg-paper-base"
            role="progressbar"
            aria-valuenow={Math.round(percent)}
            aria-valuemin={0}
            aria-valuemax={100}
            aria-label="内存使用率"
          >
            <div
              className={`h-full ${
                percent >= 90 ? "bg-error-text" : percent >= 75 ? "bg-warning-text" : "bg-accent-seal"
              }`}
              style={{ width: `${Math.min(100, percent)}%` }}
            />
          </div>
        </div>
      )}
      <Row
        label="口径"
        value={
          <span className="font-mono text-xs text-ink-muted">
            {host.availableBasis === "MemAvailable" ? "MemAvailable（含可回收缓存）" : "MemFree"}
          </span>
        }
      />
    </>
  );
}

// --------------------------------------------------------------------------- 小组件

function Th({ children }: { children: React.ReactNode }) {
  return <th className="px-4 py-2 font-normal">{children}</th>;
}

function Panel({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="rounded-lg border border-ink-line bg-paper-card p-4 shadow-sm">
      <h2 className="mb-3 text-sm font-semibold text-ink-primary">{title}</h2>
      <dl className="divide-y divide-dashed divide-ink-line/70 text-sm">{children}</dl>
    </section>
  );
}

function Row({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="flex items-center justify-between gap-3 py-2">
      <dt className="text-xs text-ink-muted">{label}</dt>
      <dd className="text-ink-primary">{value}</dd>
    </div>
  );
}

/** 配额展示：``0`` = 不限，与账户页同一个口径。 */
function formatQuota(size: number): string {
  return size <= 0 ? "不限" : formatBytes(size);
}
