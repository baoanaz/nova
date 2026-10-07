/**
 * 控制台首页（TASK-071；TASK-100 按用户 2026-09-14 要求精简）。
 *
 * 精简口径（用户原话："用户只是看数据，不需要知道这么多东西……保持高级感，专业"）：
 *
 * - **「索引」与「使用次数」合并成「工具调用」**：用户不知道索引/检索的内部概念，
 *   他只知道"我调了一次 Tool，成功还是失败，花了多久"。因此只留三个数字：
 *   成功、失败、平均耗时；
 * - **「项目」面板移出本页**（→ `/projects`）：项目是"管理"而非"看数据"，
 *   放同一页会让控制台变成大杂烩；
 * - **新增「服务模型」面板**：LLM 与 embedding 的最小必要信息
 *   （从设置页移来——它们是"当前生效值"，属于首屏该看的东西）；
 * - **删页脚版本说明**（`nova-web（Module 07 / D-40）…`）：用户不需要知道渲染链路。
 *
 * 保留「账户资料」：它是本页唯一的身份信息，用户需要确认"我是谁"。
 *
 * TASK-110（2026-09-15 用户要求）：把独立的「账户」页**合并回控制台**（两者重合），
 * 并把身份/特权/额度都长在「账户资料」卡里，头部显示 ``ID #001``。
 * 因此 ``/account`` 路由已重定向到 ``/``，``AccountPage.tsx`` 已删除——
 * 同一份信息不再有两个页面各渲染一遍。
 *
 * 面板口径（不许美化成好看的数字）：
 * - 未测量的一律显示 `—`（如 `citationCoverageAvg` 在 LLM 接入前恒为 null）；
 * - 索引耗时与检索耗时是**两个不同口径的平均值**（前者按索引 run，后者按查询），
 *   本页如实分开标注，不做无依据的加总。
 */

import { useCallback, useEffect, useState } from "react";

import { type AccountOverview, getAccountOverview, getMe, type Account } from "../api/client";
import { ErrorBlock, LoadingBlock, Page } from "../components/ui";
import { ServiceModels } from "../components/ServiceModels";
import { Icon, type IconName } from "../components/Icon";

const WINDOW_DAYS = 30;

/**
 * 时间范围选择器（TASK-100，用户 2026-09-14："可选择范围时间，天为单位"）。
 *
 * 选项是**固定档位**而不是任意日期区间：用户要的是"看看最近怎么样"，
 * 固定档位一键切换即可；任意区间需要日期控件与后端区间查询（另开卡）。
 */
const RANGE_OPTIONS: [number, string][] = [
  [1, "今天"],
  [7, "近 7 天"],
  [30, "近 30 天"],
  [90, "近 90 天"],
];

function RangePicker({ days, onChange }: { days: number; onChange: (days: number) => void }) {
  return (
    <div className="range-picker" aria-label="统计时间范围">
      {RANGE_OPTIONS.map(([value, label]) => (
        <button
          key={value}
          type="button"
          onClick={() => onChange(value)}
          aria-pressed={days === value}
          className={days === value ? "bg-accent-seal text-accent-contrast" : "text-ink-muted hover:bg-paper-base"}
        >
          {label}
        </button>
      ))}
    </div>
  );
}

export function DashboardPage() {
  const [data, setData] = useState<AccountOverview | null>(null);
  //: 身份细节（头衔/编号/能力位）。与 overview 分开取：那个端点是“用量”，不含能力位。
  const [account, setAccount] = useState<Account | null>(null);
  const [error, setError] = useState<unknown>(null);
  //: 时间窗口（天）。用户 2026-09-14 要求"可选择范围时间，天为单位"。
  const [days, setDays] = useState(WINDOW_DAYS);

  const load = useCallback(async () => {
    setError(null);
    try {
      // 两个请求并行：``overview`` 给用量，``me`` 给身份与能力位（两件事，不互相依赖）。
      const [overview, me] = await Promise.all([
        getAccountOverview(days),
        getMe().catch(() => null),
      ]);
      setData(overview);
      setAccount(me);
    } catch (err) {
      setData(null);
      setError(err);
    }
  }, [days]);

  useEffect(() => {
    void load();
  }, [load]);

  if (error !== null) return <ErrorBlock error={error} />;
  if (data === null) return <LoadingBlock />;

  const { account: profile, index, usage } = data;

  return (
    <Page>
      <div className="flex flex-wrap items-baseline justify-between gap-3">
        <h1 className="text-lg font-semibold">控制台</h1>
        <RangePicker days={days} onChange={setDays} />
      </div>

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
        <Panel title="账户资料" icon="user">
          <Row label="账户" value={profile.name} />
          <Row
            label="ID"
            value={
              account?.userNo == null
                ? "—"
                : `#${String(account.userNo).padStart(3, "0")}`
            }
          />
          <Row label="身份" value={<TitleBadge account={account} />} />
          <Row label="类型" value={profile.isLocal ? "本地单用户" : "云端账户"} />
          <Row label="创建时间" value={formatTime(profile.createdAt)} />
          <Row label="项目数" value={String(profile.projectCount)} />
          <Row
            label="空间上限"
            value={formatQuota(account?.capabilities.quotaBytes ?? 0)}
          />
          <Row
            label="自定义 Key"
            value={
              account?.capabilities.canCustomKey ? (
                <span className="text-success-text">可用（{account.title}特权）</span>
              ) : (
                <span className="text-ink-muted">拓荒者专属</span>
              )
            }
          />
        </Panel>

        <ServiceModels />
      </div>

      <Panel title={`工具调用（近 ${data.days} 天）`} icon="activity">
        {/* 保留三类统计与原有口径，每个指标用独立数据框展示。 */}
        <StatRow
          label="仓库初始化"
          items={[
            ["次数", `${index.total} 次`],
            [
              "平均耗时",
              index.avgDurationMs === null ? "—" : formatDuration(index.avgDurationMs),
            ],
            [
              "最快 / 最慢",
              index.minDurationMs == null
                ? "—"
                : `${formatDuration(index.minDurationMs)} / ${formatDuration(index.maxDurationMs ?? 0)}`,
            ],
          ]}
        />
        <StatRow
          label="检索"
          items={[
            ["次数", `${usage.total} 次`],
            ["平均耗时", usage.avgLatencyMs === null ? "—" : `${usage.avgLatencyMs} ms`],
          ]}
        />
        <StatRow
          label="Tool 调用"
          items={[
            ["成功", String(usage.succeeded + index.succeeded), "ok"],
            ["失败", String(usage.failed + index.failed), "bad"],
          ]}
        />

        {usage.total === 0 && index.total === 0 && (
          <p className="mt-3 text-xs text-ink-muted">
            还没有调用记录——在 Agent 里问一句代码相关的问题，这里就会出现数据。
          </p>
        )}
      </Panel>
    </Page>
  );
}

function Panel({ title, icon, children }: { title: string; icon: IconName; children: React.ReactNode }) {
  return (
    <section className="min-w-0 border-2 border-ink-line bg-paper-card p-5 shadow-sm md:p-6">
      <h2 className="panel-heading mb-5 text-base font-semibold text-ink-primary"><Icon name={icon} />{title}</h2>
      {children}
    </section>
  );
}

/** 工具调用按类别分组，每项指标独立成框，保留未测量值和成功／失败语义。 */
function StatRow({
  label,
  items,
}: {
  label: string;
  items: [string, string, ("ok" | "bad")?][];
}) {
  return (
    <section className="mb-5 border-b border-dashed border-ink-line pb-5 last:mb-0 last:border-b-0 last:pb-0">
      <h3 className="mb-3 text-sm font-semibold text-ink-primary">{label}</h3>
      <dl className={`grid gap-3 ${items.length === 3 ? "sm:grid-cols-3" : "sm:grid-cols-2"}`}>
        {items.map(([key, value, tone]) => (
          <div
            key={key}
            className="min-w-0 rounded-lg border border-ink-line bg-paper-raised p-4"
          >
            <dt className="text-xs text-ink-muted">{key}</dt>
            <dd
              className={`mt-2 break-words font-mono text-xl font-semibold leading-snug tabular-nums ${
                tone === "ok"
                  ? "text-success-text"
                  : tone === "bad"
                    ? "text-error-text"
                    : "text-ink-primary"
              }`}
            >
              {value}
            </dd>
          </div>
        ))}
      </dl>
    </section>
  );
}

/**
 * 身份徽章：``拓荒者 #027`` / ``执炬者`` / ``旅人``（TASK-110）。
 *
 * 编号展示用的是**内测收藏品编号**（``earlyMemberNo``，仅前 100 名有）；
 * 全站顺序号（``userNo``）在「ID」行单独展示（用户 2026-09-15 要求 ``ID：#001``）。
 */
export function TitleBadge({ account }: { account: Account | null }) {
  if (account === null || account.isLocal) return null;
  const withNumber =
    account.earlyMemberNo === null
      ? account.title
      : `${account.title} #${String(account.earlyMemberNo).padStart(3, "0")}`;
  const tone =
    account.role === "admin"
      ? "border-ink-control bg-paper-raised text-ink-primary"
      : account.role === "beta"
        ? "border-brand-gold/60 bg-paper-raised text-brand-gold"
        : "border-ink-line bg-paper-base text-ink-muted";
  return (
    <span
      data-testid="title-badge"
      className={`rounded-full border px-2 py-0.5 text-xs font-medium ${tone}`}
    >
      {withNumber}
    </span>
  );
}

/**
 * 字节 → 人读（与后端 ``quota.format_bytes`` 同一套单位与舍入）。
 *
 * ``0`` 展示为“不限”（TASK-094 口径：上限为 0 = 不限，而不是“限制 0 字节”）。
 * 注：本文件另有一个更宽松的 ``formatBytes``（接受 null，显示 ``—``）；
 * 配额场景用这个（0 是合法且有意义的“不限”），空间占用场景用那个（null 是“未提供”）。
 */
export function formatQuota(size: number): string {
  if (size <= 0) return "不限";
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KiB`;
  if (size < 1024 * 1024 * 1024) return `${(size / 1024 / 1024).toFixed(1)} MiB`;
  return `${(size / 1024 / 1024 / 1024).toFixed(2)} GiB`;
}

function Row({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="flex items-baseline justify-between gap-3 border-b border-dashed border-ink-line/70 py-2.5 last:border-b-0">
      <span className="shrink-0 text-xs text-ink-muted">{label}</span>
      <span className="min-w-0 break-words text-right text-sm text-ink-primary">{value}</span>
    </div>
  );
}

export function formatTime(unixSeconds: number): string {
  if (!unixSeconds) return "—";
  return new Date(unixSeconds * 1000).toLocaleString();
}

export function formatDuration(ms: number): string {
  if (ms < 1000) return `${ms} ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)} s`;
  const minutes = Math.floor(ms / 60_000);
  const seconds = Math.round((ms % 60_000) / 1000);
  return `${minutes}m ${seconds}s`;
}

/** 字节数格式化；`null`/`undefined`（后端未提供）显示 `—`，不退化成"0 B"。 */
export function formatBytes(bytes: number | null | undefined): string {
  if (!bytes) return "—";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KiB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MiB`;
  return `${(bytes / 1024 / 1024 / 1024).toFixed(2)} GiB`;
}
