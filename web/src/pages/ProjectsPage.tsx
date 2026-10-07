/**
 * 项目页（TASK-100 §需求4）：存储配额总览 + 项目列表与删除。
 *
 * 为什么从控制台拆出来（用户原话）："用户端不关心索引和使用的细节……
 * 新增一个与控制台同级别的页面叫项目，里面可以看见存储配额，可以管理删除某个项目，
 * 来释放存储空间"。
 *
 * 页面结构（用户指定的信息层级）：
 * 1. **用户总上限进度条**——一眼看到"我用了多少 / 还剩多少"（这是本页的主目的：
 *    释放空间前先知道够不够用）；
 * 2. **单项目上限说明文字**——不是进度条，是一句说明（它是配置事实，不是当前状态）；
 * 3. **项目表**——每个项目的占用 + 删除入口（删除只影响索引数据，源码不受影响）。
 *
 * 删除纪律（承接 TASK-094 §D，本页只换位置不改行为）：
 * - 二次确认弹窗；**取消不发任何请求**；
 * - 删除后**重新拉取**整个概览（不能只从本地数组滤掉——占用/配额都变了，
 *   本地删行会留下与后端不一致的"残影"）；
 * - 失败如实报错并保留弹窗（404 / 网络错误都不静默）。
 */

import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";

import {
  type AccountOverview,
  type QuotaStatusView,
  deleteProject,
  getAccountOverview,
} from "../api/client";
import { ConfirmDialog, EmptyState, ErrorBlock, LoadingBlock, Page } from "../components/ui";

const WINDOW_DAYS = 30;

/** 待删除的项目（非 null 时弹二次确认；**不直接发请求**）。 */
type PendingDelete = { projectId: string; name: string };

/**
 * 仓库名（去掉 `@分支` 后缀）。
 *
 * TASK-111 后 `displayName` 形如 `nova@feature/x`，分支已单独成列；仓库列再带上它
 * 会让同一仓库的多个分支看起来是两个仓库，反而看不出“该不该删一个”。
 * 没有后缀时原样返回（无 git 的项目、旧数据）。
 */
function repoName(displayName: string | undefined, fallback: string): string {
  if (!displayName) return fallback;
  const at = displayName.lastIndexOf("@");
  return at > 0 ? displayName.slice(0, at) : displayName;
}

export function ProjectsPage() {
  const [data, setData] = useState<AccountOverview | null>(null);
  const [error, setError] = useState<unknown>(null);
  //: 删除流程的独立状态：**不要**复用页面级 `error`，否则一次删除失败会把整页换成错误页。
  const [pending, setPending] = useState<PendingDelete | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState<unknown>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      setData(await getAccountOverview(WINDOW_DAYS));
    } catch (err) {
      setData(null);
      setError(err);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const confirmDelete = useCallback(async () => {
    if (pending === null) return;
    setDeleting(true);
    setDeleteError(null);
    try {
      await deleteProject(pending.projectId);
      setPending(null);
      await load();
    } catch (err) {
      // 失败如实报错（404/网络错误都不静默）：保留弹窗，让用户看到原因再决定。
      setDeleteError(err);
    } finally {
      setDeleting(false);
    }
  }, [pending, load]);

  if (error !== null) return <ErrorBlock error={error} />;
  if (data === null) return <LoadingBlock />;

  const { storage, projects } = data;

  return (
    <Page>
      <div>
        <h1 className="text-lg font-semibold">项目</h1>
        <p className="mt-1 text-sm text-ink-muted">
          索引数据的占用与清理。删除只影响索引数据，源码文件不受影响。
        </p>
      </div>

      {storage !== undefined ? (
        <QuotaPanel storage={storage} />
      ) : (
        <p className="text-xs text-ink-muted">后端未提供配额信息（旧版本服务）。</p>
      )}

      <section className="rounded-lg border border-ink-line bg-paper-card p-4 shadow-sm">
        {deleteError !== null && (
          <div className="mb-3">
            <ErrorBlock error={deleteError} />
          </div>
        )}

        {projects.length === 0 ? (
          <EmptyState
            title="还没有项目"
            hint="项目在你第一次调用 Tool 时自动创建：接入 Agent 并让它索引一个仓库，项目就会出现在这里。"
            action={
              <Link className="text-xs underline" to="/connect">
                去接入指南
              </Link>
            }
          />
        ) : (
          <table className="w-full border-collapse text-sm">
            <thead>
              <tr className="text-left text-xs text-ink-muted">
                <th className="border-b border-ink-line py-1">仓库</th>
                <th className="border-b border-ink-line py-1" title="同一仓库的不同分支是不同项目">
                  分支
                </th>
                <th className="border-b border-ink-line py-1">projectId</th>
                <th className="border-b border-ink-line py-1">文件</th>
                <th className="border-b border-ink-line py-1">chunks</th>
                <th className="border-b border-ink-line py-1">状态</th>
                <th className="border-b border-ink-line py-1" title="索引数据磁盘占用">
                  占用
                </th>
                <th className="border-b border-ink-line py-1">操作</th>
              </tr>
            </thead>
            <tbody>
              {projects.map((project) => (
                <tr key={project.projectId}>
                  <td className="border-b border-ink-line/60 py-1">
                    {repoName(project.displayName, project.projectId)}
                  </td>
                  {/* 分支（TASK-111）：没有分支信息时如实显示 `—`，不编一个“main”。 */}
                  <td
                    className="border-b border-ink-line/60 py-1 font-mono text-xs"
                    data-testid={`branch-${project.projectId}`}
                  >
                    {project.branch ?? "—"}
                  </td>
                  <td className="border-b border-ink-line/60 py-1 font-mono text-xs">
                    {project.projectId}
                  </td>
                  <td className="border-b border-ink-line/60 py-1">
                    {project.sync?.filesIndexed ?? "—"}
                  </td>
                  <td className="border-b border-ink-line/60 py-1">{project.sync?.chunks ?? "—"}</td>
                  <td className="border-b border-ink-line/60 py-1 text-xs">
                    {project.indexProgress?.state ?? "—"}
                  </td>
                  {/* 占用：后端未提供时 `—`（不把"未测量"伪装成 0 B）。 */}
                  <td
                    className="border-b border-ink-line/60 py-1"
                    data-testid={`disk-${project.projectId}`}
                  >
                    {project.diskBytes == null ? "—" : formatBytes(project.diskBytes)}
                  </td>
                  <td className="border-b border-ink-line/60 py-1">
                    <button
                      type="button"
                      onClick={() =>
                        setPending({
                          projectId: project.projectId,
                          name: project.displayName || project.projectId,
                        })
                      }
                      className="rounded border border-danger-base/40 px-2 py-0.5 text-xs text-danger-base hover:bg-danger-base/10"
                    >
                      删除
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      <ConfirmDialog
        open={pending !== null}
        title={`删除项目 ${pending?.name ?? ""}？`}
        confirmLabel="删除索引数据"
        busy={deleting}
        onConfirm={() => void confirmDelete()}
        onCancel={() => {
          // 取消**不发请求**（只关弹窗）；顺手清掉上一次的删除错误，避免下次点开时残留。
          setPending(null);
          setDeleteError(null);
        }}
      >
        <p>
          将删除该项目的<strong>全部索引数据</strong>（含向量与同步账本）。
        </p>
        <p>源码文件不受影响；下次调用 Tool 时会重新上传并索引。</p>
        <p className="text-xs text-ink-muted">
          projectId：<code className="font-mono">{pending?.projectId ?? ""}</code>
        </p>
      </ConfirmDialog>
    </Page>
  );
}

/**
 * 配额面板：**用户总上限用进度条**（本页主目的：释放空间前先知道够不够用），
 * **单项目上限用说明文字**（用户 2026-09-14 明确要求——它是配置事实，不是当前状态）。
 */
function QuotaPanel({ storage }: { storage: QuotaStatusView }) {
  const { user, project, warnRatio } = storage;
  const tone =
    user.status === "exceeded"
      ? { bar: "bg-error-text", text: "text-error-text", label: "已超出上限" }
      : user.status === "warning"
        ? { bar: "bg-warning-text", text: "text-warning-text", label: "接近上限" }
        : { bar: "bg-success-text", text: "text-success-text", label: "余量充足" };

  return (
    <section
      className="rounded-lg border border-ink-line bg-paper-card p-4 shadow-sm"
      data-testid="storage-quota"
    >
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h2 className="text-sm font-semibold text-ink-primary">存储配额</h2>
        <span className={`text-sm font-medium ${user.unlimited ? "text-ink-muted" : tone.text}`}>
          {user.unlimited
            ? `已用 ${formatBytes(user.usedBytes)} · 未设上限`
            : `${formatBytes(user.usedBytes)} / ${formatBytes(user.limitBytes)}`}
          {!user.unlimited && user.ratio !== null && `（${(user.ratio * 100).toFixed(0)}%）`}
        </span>
      </div>

      {!user.unlimited && (
        <div className="mt-2 h-2 w-full overflow-hidden rounded bg-paper-base">
          <div
            className={`h-full ${tone.bar}`}
            // 宽度封顶 100%：超限时进度条不能撑破容器（比例由文字如实给出）。
            style={{ width: `${Math.min(100, (user.ratio ?? 0) * 100).toFixed(1)}%` }}
          />
        </div>
      )}

      {!user.unlimited && user.status !== "ok" && (
        <p className={`mt-2 text-xs ${tone.text}`}>
          {tone.label}：删除下方不再需要的项目即可释放空间（达到 {Math.round(warnRatio * 100)}% 起提示；
          超限不阻断索引与检索）。
        </p>
      )}

      {/* 单项目上限：说明文字而非进度条（用户指定）。 */}
      <p className="mt-3 border-t border-dashed border-ink-line/60 pt-2 text-xs text-ink-muted">
        单个项目上限：
        {project.unlimited ? (
          "不限"
        ) : (
          <>
            <strong className="text-ink-primary">{formatBytes(project.limitBytes)}</strong>
            ，达到上限的新文件将不再入库（已索引内容仍可检索）。
          </>
        )}
      </p>
    </section>
  );
}

/** 字节数格式化；`null`/`undefined`（后端未提供）显示 `—`，不退化成"0 B"。 */
function formatBytes(bytes: number | null | undefined): string {
  if (!bytes) return "—";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KiB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MiB`;
  return `${(bytes / 1024 / 1024 / 1024).toFixed(2)} GiB`;
}
