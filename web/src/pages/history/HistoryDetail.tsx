/** 历史记录详情：耗时构成 → Tool 输入 / 输出 → 元信息。原生 <dialog>，Esc 与焦点陷阱由浏览器负责。 */

import { useEffect, useRef, type ReactNode } from "react";

import { CopyButton } from "../../components/ui";
import { formatDuration, formatTime } from "../DashboardPage";
import { KindBadge, StateBadge } from "./badges";
import type { ActivityRow, InitBatch, StageBreakdown, StageRow } from "./model";

export function HistoryDetail({ row, onClose }: { row: ActivityRow | null; onClose: () => void }) {
  const ref = useRef<HTMLDialogElement | null>(null);

  useEffect(() => {
    const dialog = ref.current;
    if (dialog === null) return;
    if (row !== null && !dialog.open) dialog.showModal();
    if (row === null && dialog.open) dialog.close();
  }, [row]);

  return (
    <dialog
      ref={ref}
      onCancel={(event) => {
        event.preventDefault();
        onClose();
      }}
      onClick={(event) => {
        if (event.target === ref.current) onClose();
      }}
      className="w-[min(94vw,56rem)] max-h-[90vh] overflow-hidden border border-ink-line bg-paper-card p-0 shadow-xl backdrop:bg-black/60"
    >
      {row !== null && (
        <div className="flex max-h-[90vh] flex-col">
          <header className="flex flex-wrap items-start justify-between gap-3 border-b border-ink-line bg-paper-raised px-5 py-4">
            <div className="min-w-0">
              <h2 className="flex flex-wrap items-center gap-2 text-base font-semibold text-ink-primary">
                <KindBadge kind={row.kind} />
                <span className="truncate">{row.project}</span>
                <StateBadge state={row.state} />
              </h2>
              <p className="mt-1 text-xs text-ink-muted">{formatTime(row.at)}</p>
            </div>
            <div className="text-right">
              <div className="text-xs text-ink-muted">总耗时</div>
              <div className="font-mono text-xl font-semibold tabular-nums text-ink-primary">
                {formatDuration(row.durationMs)}
              </div>
            </div>
          </header>

          <div className="space-y-5 overflow-y-auto px-5 py-4">
            {row.kind === "init" ? (
              <InitTiming row={row} />
            ) : (
              <p className="text-xs text-ink-muted">
                服务端只记录了整次请求的耗时（{formatDuration(row.durationMs)}），没有拆分检索 / LLM 的分段耗时。
              </p>
            )}

            <div className="grid gap-4 lg:grid-cols-2">
              <CodeBlock label="Tool 输入" text={row.input} />
              <CodeBlock label="Tool 输出" text={row.output} />
            </div>

            <dl className="grid gap-x-6 gap-y-1 border-t border-ink-line/70 pt-3 sm:grid-cols-2">
              {row.metrics.map((item) => (
                <div key={item.label} className="flex min-w-0 items-baseline justify-between gap-3">
                  <dt className="shrink-0 text-xs text-ink-muted">{item.label}</dt>
                  <dd className="min-w-0 break-all text-right font-mono text-xs text-ink-primary">
                    {item.value}
                  </dd>
                </div>
              ))}
              {row.traceId && (
                <div className="flex min-w-0 items-center justify-between gap-3">
                  <dt className="shrink-0 text-xs text-ink-muted">trace id</dt>
                  <dd className="flex min-w-0 items-center gap-2">
                    <code className="break-all font-mono text-xs text-ink-primary">{row.traceId}</code>
                    <CopyButton text={row.traceId} label="复制" />
                  </dd>
                </div>
              )}
            </dl>

            {row.note && (
              <p className="border border-ink-line bg-paper-base px-3 py-2 text-xs text-ink-muted">
                {row.note}
              </p>
            )}
          </div>

          <footer className="flex justify-end border-t border-ink-line px-5 py-3">
            <button
              type="button"
              onClick={onClose}
              className="rounded border border-ink-line px-4 py-1.5 text-sm text-ink-primary hover:bg-paper-base"
            >
              关闭
            </button>
          </footer>
        </div>
      )}
    </dialog>
  );
}

function Section({ title, aside, children }: { title: string; aside?: ReactNode; children: ReactNode }) {
  return (
    <section>
      <div className="mb-2 flex items-baseline justify-between gap-3">
        <h3 className="text-sm font-semibold text-ink-primary">{title}</h3>
        {aside}
      </div>
      {children}
    </section>
  );
}

function CodeBlock({ label, text }: { label: string; text: string }) {
  return (
    <div className="min-w-0">
      <div className="mb-1 flex items-center justify-between">
        <span className="text-xs text-ink-muted">{label}</span>
        <CopyButton text={text} label="复制" />
      </div>
      <pre className="max-h-72 overflow-auto border border-ink-line bg-paper-base p-3 font-mono text-xs leading-relaxed whitespace-pre-wrap break-words text-ink-primary">
        {text}
      </pre>
    </div>
  );
}

// ---------------------------------------------------------------- 初始化：耗时构成

function InitTiming({ row }: { row: ActivityRow }) {
  const batches = row.batches ?? [];
  return (
    <>
      <Section
        title="各阶段耗时"
        aside={
          row.coarseDuration ? (
            <span className="text-xs text-ink-muted">旧记录：总耗时只有整秒精度</span>
          ) : undefined
        }
      >
        {row.stages === undefined ? (
          <p className="text-xs text-ink-muted">这条记录产生于阶段计时落库之前，没有分阶段数据。</p>
        ) : (
          <Waterfall stages={row.stages} totalMs={row.durationMs} />
        )}
      </Section>
      {batches.length > 1 && <Batches batches={batches} />}
    </>
  );
}

function pct(part: number, total: number): string {
  if (total <= 0) return "0%";
  const value = (part / total) * 100;
  return value < 1 ? "<1%" : `${Math.round(value)}%`;
}

function Waterfall({ stages, totalMs }: { stages: StageBreakdown; totalMs: number }) {
  // 分母取「总耗时」与「顶层阶段之和」中较大者，条形永远不会超出。
  const scale = Math.max(totalMs, stages.topTotalMs, 1);
  const other = totalMs - stages.topTotalMs;
  const showOther = other > Math.max(50, totalMs * 0.03);

  return (
    <div className="border border-ink-line bg-paper-raised p-3">
      <ul className="space-y-2">
        {stages.top.map((stage) => (
          <li key={stage.name}>
            <StageLine stage={stage} scale={scale} />
            {stage.name === "write_batch" && stages.writeDetail.length > 0 && (
              <ul className="mt-1.5 space-y-1.5 border-l border-ink-line pl-3 ml-1">
                {stages.writeDetail.map((sub) => (
                  <li key={sub.name}>
                    <StageLine stage={sub} scale={scale} nested />
                  </li>
                ))}
              </ul>
            )}
          </li>
        ))}
        {showOther && (
          <li>
            <StageLine
              stage={{ name: "other", label: "其他（排队、接收上传内容等）", wallMs: other, cpuMs: 0 }}
              scale={scale}
              muted
            />
          </li>
        )}
      </ul>

      <details className="mt-3 border-t border-ink-line/70 pt-2">
        <summary className="cursor-pointer text-xs text-ink-muted">原始阶段数据（墙钟 / CPU）</summary>
        <table className="mt-2 w-full text-xs">
          <thead>
            <tr className="text-left text-ink-muted">
              <th className="py-1 pr-3 font-normal">阶段</th>
              <th className="py-1 pr-3 text-right font-normal">墙钟</th>
              <th className="py-1 text-right font-normal">CPU</th>
            </tr>
          </thead>
          <tbody>
            {stages.raw.map((stage) => (
              <tr key={stage.name} className="border-t border-ink-line/50">
                <td className="py-1 pr-3 font-mono text-ink-primary">{stage.name}</td>
                <td className="py-1 pr-3 text-right font-mono tabular-nums">{formatDuration(Math.round(stage.wallMs))}</td>
                <td className="py-1 text-right font-mono tabular-nums text-ink-muted">
                  {formatDuration(Math.round(stage.cpuMs))}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        <p className="mt-2 text-xs text-ink-muted">
          墙钟远大于 CPU 说明在等待磁盘 / 网络（如 embedding）；部分阶段相互嵌套或并行，不能直接相加。
        </p>
      </details>
    </div>
  );
}

function StageLine({
  stage,
  scale,
  nested = false,
  muted = false,
}: {
  stage: StageRow;
  scale: number;
  nested?: boolean;
  muted?: boolean;
}) {
  const width = Math.max(0.5, Math.min(100, (stage.wallMs / scale) * 100));
  return (
    <div>
      <div className="flex items-baseline justify-between gap-3">
        <span className={nested || muted ? "text-xs text-ink-muted" : "text-sm text-ink-primary"}>
          {stage.label}
        </span>
        <span className="shrink-0 font-mono text-xs tabular-nums text-ink-primary">
          {formatDuration(Math.round(stage.wallMs))}
          <span className="ml-2 text-ink-muted">{pct(stage.wallMs, scale)}</span>
        </span>
      </div>
      <div className={`mt-1 bg-paper-base ${nested ? "h-1" : "h-1.5"}`} aria-hidden>
        <div
          className={`h-full ${muted ? "bg-ink-line" : nested ? "bg-ink-subtle" : "bg-accent-seal"}`}
          style={{ width: `${width}%` }}
        />
      </div>
    </div>
  );
}

function Batches({ batches }: { batches: InitBatch[] }) {
  return (
    <Section title={`上传批次（${batches.length} 批）`}>
      <div className="overflow-x-auto border border-ink-line">
        <table className="w-full text-xs">
          <thead>
            <tr className="text-left text-ink-muted">
              <th className="px-3 py-1.5 font-normal">批次</th>
              <th className="px-3 py-1.5 text-right font-normal">文件</th>
              <th className="px-3 py-1.5 text-right font-normal">chunks</th>
              <th className="px-3 py-1.5 text-right font-normal">耗时</th>
              <th className="px-3 py-1.5 font-normal">结果</th>
            </tr>
          </thead>
          <tbody>
            {batches.map((batch, index) => (
              <tr key={batch.runId} className="border-t border-ink-line/60">
                <td className="px-3 py-1.5">#{index + 1}</td>
                <td className="px-3 py-1.5 text-right font-mono tabular-nums">
                  {batch.files} / {batch.totalFiles}
                </td>
                <td className="px-3 py-1.5 text-right font-mono tabular-nums">{batch.chunks}</td>
                <td className="px-3 py-1.5 text-right font-mono tabular-nums">
                  {formatDuration(batch.durationMs)}
                </td>
                <td className={`px-3 py-1.5 ${batch.failed ? "text-error-text" : "text-success-text"}`}>
                  {batch.failed ? (batch.error ?? "失败") : "成功"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Section>
  );
}
