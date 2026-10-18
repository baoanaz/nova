/**
 * 历史记录：每次初始化 / search / ask 的耗时、输入、输出。
 *
 * - 顶部三张汇总卡（同时是类型筛选）：次数、平均 / 最慢耗时、异常数；
 * - 列表：时间 · 类型 · 项目 · 摘要 · 结果 · 耗时（带相对长度条）· 体量；点一行看详情；
 * - 详情：初始化的分阶段耗时瀑布，检索 / ask 的原始输入输出（见 history/HistoryDetail）。
 *
 * 数据口径：
 * - 初始化按 callId 折叠（一次逻辑初始化可能分多批上传，耗时与文件数求和）；
 * - 索引明细端点不支持按天过滤，这里前端过滤；检索走后端 `days`；
 * - 刷新一律静默，不清空已有数据；单个项目读取失败会明确列出，不当作「没有数据」。
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";

import {
  type IndexRun,
  type Project,
  type UsageSummary,
  getProjectIndexRuns,
  getUsageSummary,
  listProjects,
} from "../api/client";
import { EmptyState, ErrorBlock, LoadingBlock, Page } from "../components/ui";
import { formatDuration } from "./DashboardPage";
import { HistoryDetail } from "./history/HistoryDetail";
import { KindBadge, StateBadge } from "./history/badges";
import { type ActivityRow, KIND_LABEL, type Kind, buildRows, summarize } from "./history/model";

const ROW_LIMIT = 200;
/** 自动刷新间隔（毫秒），常开、无开关。 */
const AUTO_REFRESH_MS = 10_000;

const RANGE_OPTIONS: [number, string][] = [
  [1, "今天"],
  [7, "近 7 天"],
  [30, "近 30 天"],
  [90, "近 90 天"],
];

const KINDS: Kind[] = ["init", "search", "ask"];

type RunReadFailure = { projectId: string; error: unknown };

export function HistoryPage() {
  const [days, setDays] = useState(30);
  const [projects, setProjects] = useState<Project[] | null>(null);
  const [runs, setRuns] = useState<{ projectId: string; run: IndexRun }[] | null>(null);
  const [runFailures, setRunFailures] = useState<RunReadFailure[]>([]);
  const [usage, setUsage] = useState<UsageSummary | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [detail, setDetail] = useState<ActivityRow | null>(null);
  const [kind, setKind] = useState<Kind | "all">("all");
  const [onlyIssues, setOnlyIssues] = useState(false);
  const [refreshedAt, setRefreshedAt] = useState<number | null>(null);
  const [loading, setLoading] = useState(false);

  /** `rows` 的镜像：`load` 需要知道屏幕上是否已有数据，但不能把 `rows` 放进依赖（会重挂定时器）。 */
  const rowsRef = useRef<ActivityRow[] | null>(null);

  const load = useCallback(
    async (options: { silent?: boolean } = {}) => {
      // 已经渲染过数据时，任何刷新都不再白屏。
      const silent = options.silent ?? rowsRef.current !== null;
      setError(null);
      if (!silent) setRunFailures([]);
      setLoading(true);
      try {
        const listed = await listProjects();
        setProjects(listed);
        const perProject = await Promise.all(
          listed.map(async (project) => {
            try {
              const items = await getProjectIndexRuns(project.projectId, ROW_LIMIT);
              return { items: items.map((run) => ({ projectId: project.projectId, run })) };
            } catch (err) {
              return { items: [], error: { projectId: project.projectId, error: err } };
            }
          }),
        );
        setRunFailures(perProject.flatMap((item) => (item.error === undefined ? [] : [item.error])));
        setRuns(
          perProject
            .flatMap((item) => item.items)
            .sort((left, right) => right.run.finishedAt - left.run.finishedAt)
            .slice(0, ROW_LIMIT),
        );
        setUsage(await getUsageSummary(days, ROW_LIMIT));
        setRefreshedAt(Date.now());
      } catch (err) {
        // 刷新失败不清空已有数据，只显示错误横幅。
        setError(err);
      } finally {
        setLoading(false);
      }
    },
    [days],
  );

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    const timer = window.setInterval(() => void load({ silent: true }), AUTO_REFRESH_MS);
    return () => window.clearInterval(timer);
  }, [load]);

  const nameOf = useCallback(
    (projectId: string) =>
      projects?.find((item) => item.projectId === projectId)?.displayName || projectId,
    [projects],
  );

  const rows = useMemo<ActivityRow[] | null>(() => {
    if (runs === null || usage === null) return null;
    const since = Math.floor(Date.now() / 1000) - days * 86400;
    const active = (projects ?? []).map((item) => ({
      projectId: item.projectId,
      progress: item.indexProgress ?? null,
    }));
    return buildRows(runs, usage.recent, since, nameOf, active);
  }, [runs, usage, days, nameOf, projects]);
  rowsRef.current = rows;

  const visible = useMemo(
    () =>
      (rows ?? []).filter(
        (row) =>
          (kind === "all" || row.kind === kind) &&
          (!onlyIssues || row.state === "failed" || row.state === "degraded"),
      ),
    [rows, kind, onlyIssues],
  );
  const slowest = useMemo(() => Math.max(1, ...visible.map((row) => row.durationMs)), [visible]);

  return (
    <Page className="history-page">
      <div className="flex flex-wrap items-baseline justify-between gap-3">
        <div className="flex flex-wrap items-baseline gap-3">
          <h1 className="text-lg font-semibold">历史记录</h1>
          <button
            type="button"
            onClick={() => void load({ silent: true })}
            disabled={loading}
            className="rounded border border-ink-line px-2.5 py-1 text-xs text-ink-primary hover:bg-paper-base disabled:opacity-50"
          >
            {loading ? "刷新中…" : "刷新"}
          </button>
          {refreshedAt !== null && (
            <span className="text-xs text-ink-muted">
              数据更新于：{new Date(refreshedAt).toLocaleTimeString()}
            </span>
          )}
        </div>
        <div className="range-picker" aria-label="时间范围">
          {RANGE_OPTIONS.map(([value, label]) => (
            <button
              key={value}
              type="button"
              onClick={() => setDays(value)}
              aria-pressed={days === value}
              className={
                days === value ? "bg-accent-seal text-accent-contrast" : "text-ink-muted hover:bg-paper-base"
              }
            >
              {label}
            </button>
          ))}
        </div>
      </div>

      {error !== null && <ErrorBlock error={error} />}

      {runFailures.length > 0 && (
        <div className="space-y-2 border border-warning-line bg-warning-soft p-3">
          <p className="text-sm font-medium text-warning-text">
            部分项目的初始化记录读取失败：{runFailures.map((item) => nameOf(item.projectId)).join("、")}
            。下表只包含读取成功的项目。
          </p>
          {runFailures.map((item) => (
            <ErrorBlock key={item.projectId} error={item.error} />
          ))}
        </div>
      )}

      {rows === null ? (
        <LoadingBlock />
      ) : (
        <>
          <div className="mt-4 grid gap-3 sm:grid-cols-3" role="group" aria-label="按类型筛选">
            {KINDS.map((item) => (
              <SummaryCard
                key={item}
                kind={item}
                rows={rows}
                active={kind === item}
                onToggle={() => setKind(kind === item ? "all" : item)}
              />
            ))}
          </div>

          <section className="dashboard-panel mt-4 min-w-0 border-2 border-ink-line bg-paper-card shadow-sm">
            <div className="flex flex-wrap items-center justify-between gap-3 border-b border-ink-line px-4 py-3">
              <h2 className="text-sm font-semibold text-ink-primary">
                {kind === "all" ? "全部记录" : KIND_LABEL[kind]}
                <span className="ml-2 font-normal text-ink-muted">{visible.length} 条</span>
              </h2>
              <button
                type="button"
                aria-pressed={onlyIssues}
                onClick={() => setOnlyIssues(!onlyIssues)}
                className={`border px-2.5 py-1 text-xs ${
                  onlyIssues
                    ? "border-error-line bg-error-soft text-error-text"
                    : "border-ink-line text-ink-muted hover:bg-paper-base"
                }`}
              >
                只看异常
              </button>
            </div>

            {visible.length === 0 ? (
              <EmptyState
                title={rows.length === 0 ? `近 ${days} 天没有记录` : "没有符合筛选条件的记录"}
                hint={
                  rows.length === 0
                    ? "在 Agent 里接入 NOVA 并提一个代码问题，初始化、search、ask 都会记在这里。"
                    : "换一个类型，或关闭「只看异常」。"
                }
                action={
                  rows.length === 0 ? (
                    <Link className="text-xs underline" to="/connect">
                      去接入指南
                    </Link>
                  ) : undefined
                }
              />
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full border-collapse text-sm">
                  <thead>
                    <tr className="text-left text-xs">
                      <th className="whitespace-nowrap px-4 py-2 font-normal">时间</th>
                      <th className="whitespace-nowrap px-3 py-2 font-normal">类型</th>
                      <th className="whitespace-nowrap px-3 py-2 font-normal">项目</th>
                      <th className="px-3 py-2 font-normal">内容</th>
                      <th className="whitespace-nowrap px-3 py-2 font-normal">结果</th>
                      <th className="whitespace-nowrap px-3 py-2 text-right font-normal">耗时</th>
                      <th className="whitespace-nowrap px-3 py-2 text-right font-normal">体量</th>
                      <th className="px-3 py-2 font-normal">
                        <span className="sr-only">详情</span>
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    {visible.map((row) => (
                      <tr
                        key={row.key}
                        onClick={() => setDetail(row)}
                        className="cursor-pointer border-t border-ink-line/60"
                      >
                        <td className="whitespace-nowrap px-4 py-2 text-xs text-ink-muted">
                          {formatShortDate(row.at)}
                        </td>
                        <td className="whitespace-nowrap px-3 py-2">
                          <KindBadge kind={row.kind} />
                        </td>
                        <td className="whitespace-nowrap px-3 py-2">{row.project}</td>
                        <td className="w-full max-w-0 px-3 py-2">
                          <span className="block truncate" title={row.summary}>
                            {row.summary}
                          </span>
                        </td>
                        <td className="whitespace-nowrap px-3 py-2">
                          <StateBadge state={row.state} />
                        </td>
                        <td className="whitespace-nowrap px-3 py-2 text-right">
                          <DurationCell ms={row.durationMs} slowest={slowest} />
                        </td>
                        <td className="whitespace-nowrap px-3 py-2 text-right font-mono text-xs tabular-nums text-ink-muted">
                          {row.volume}
                        </td>
                        <td className="whitespace-nowrap px-3 py-2">
                          <button
                            type="button"
                            onClick={(event) => {
                              event.stopPropagation();
                              setDetail(row);
                            }}
                            className="rounded border border-ink-line px-2 py-0.5 text-xs text-ink-primary hover:bg-paper-base"
                          >
                            详情
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>
        </>
      )}

      <HistoryDetail row={detail} onClose={() => setDetail(null)} />
    </Page>
  );
}

function SummaryCard({
  kind,
  rows,
  active,
  onToggle,
}: {
  kind: Kind;
  rows: ActivityRow[];
  active: boolean;
  onToggle: () => void;
}) {
  const stat = summarize(rows, kind);
  return (
    <button
      type="button"
      onClick={onToggle}
      aria-pressed={active}
      className={`min-w-0 border p-3 text-left ${
        active ? "border-accent-seal bg-accent-soft" : "border-ink-line bg-paper-raised hover:bg-paper-base"
      }`}
    >
      <div className="flex items-center justify-between gap-2">
        <KindBadge kind={kind} />
        <span className="font-mono text-xl font-semibold tabular-nums text-ink-primary">
          {stat.count}
          <span className="ml-1 text-xs font-normal text-ink-muted">次</span>
        </span>
      </div>
      <dl className="mt-2 text-xs">
        <Line label="平均耗时" value={stat.avgMs === null ? "—" : formatDuration(stat.avgMs)} />
        <Line label="最慢" value={stat.maxMs === null ? "—" : formatDuration(stat.maxMs)} />
        <Line
          label="异常"
          value={String(stat.failed)}
          tone={stat.failed > 0 ? "bad" : undefined}
        />
      </dl>
    </button>
  );
}

function Line({ label, value, tone }: { label: string; value: string; tone?: "bad" }) {
  return (
    <div className="flex items-baseline justify-between gap-3 border-t border-ink-line/60 py-1 first:border-t-0">
      <dt className="text-ink-muted">{label}</dt>
      <dd
        className={`font-mono tabular-nums ${tone === "bad" ? "text-error-text" : "text-ink-primary"}`}
      >
        {value}
      </dd>
    </div>
  );
}

/** 耗时数字 + 与当前列表最慢一条相比的长度条，一眼看出哪条慢。 */
function DurationCell({ ms, slowest }: { ms: number; slowest: number }) {
  const width = Math.max(2, Math.round((ms / slowest) * 100));
  return (
    <div className="ml-auto w-24">
      <div className="font-mono text-xs tabular-nums text-ink-primary">{formatDuration(ms)}</div>
      <div className="mt-1 h-1 bg-paper-base" aria-hidden>
        <div className="h-full bg-accent-seal" style={{ width: `${width}%` }} />
      </div>
    </div>
  );
}

/** `9/14 19:38`：列表只需要「最近发生了什么」，完整时间在详情里。 */
function formatShortDate(unixSeconds: number): string {
  if (!unixSeconds) return "—";
  const date = new Date(unixSeconds * 1000);
  const hh = String(date.getHours()).padStart(2, "0");
  const mm = String(date.getMinutes()).padStart(2, "0");
  return `${date.getMonth() + 1}/${date.getDate()} ${hh}:${mm}`;
}
