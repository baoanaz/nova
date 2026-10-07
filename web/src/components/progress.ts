/**
 * `IndexProgress` 的如实渲染（D-30 / Module 07 §2.3）。
 *
 * 三条不许妥协的规则（口径来自 `docs/handbook/getting-started/M2a-验收手册.md` §2）：
 * 1. **没有百分比**：core 的 ingest 无回调，中间态拿不到真实完成度——宁可信息少，不可信息假；
 * 2. `totalFiles` 与 `processedFiles` **不是同一量纲**（前者含二进制/构建产物，后者只数真正解析的文件），
 *    所以只并排显示，不做除法；
 * 3. `state="done"` 且 `error` 非空 = "索引完成，但这些文件有解析问题"，**不是失败**。
 */

import type { IndexProgress } from "../api/types";

export type ProgressTone = "idle" | "running" | "done" | "warning" | "failed";

export interface ProgressView {
  tone: ProgressTone;
  label: string;
  detail: string | null;
  error: string | null;
}

/** 把 `indexProgress` 归一成可渲染的视图（纯函数，便于单测）。 */
export function describeIndexProgress(
  progress: IndexProgress | null | undefined,
): ProgressView {
  if (!progress) {
    return {
      tone: "idle",
      label: "未索引",
      detail: "服务未绑定本地目录，或该项目走客户端上传（索引状态见 sync）。",
      error: null,
    };
  }

  const counts = `已处理 ${progress.processedFiles} / 共 ${progress.totalFiles} 文件`;
  const noPercent = "无百分比：core 索引过程没有进度回调，这里不伪造进度条";

  switch (progress.state) {
    case "running":
      return { tone: "running", label: "索引中", detail: `${counts}（${noPercent}）`, error: null };
    case "done":
      if (progress.error) {
        return {
          tone: "warning",
          label: "索引完成（部分文件有解析问题）",
          detail: counts,
          error: progress.error,
        };
      }
      return { tone: "done", label: "索引完成", detail: counts, error: null };
    case "failed":
      return {
        tone: "failed",
        label: "索引失败",
        detail: counts,
        error: progress.error ?? "未提供错误详情",
      };
    case "idle":
    default:
      return {
        tone: "idle",
        label: "未索引（重启后需重新 attach）",
        detail: counts,
        error: null,
      };
  }
}

/*
 * 徽标配色（TASK-098 §D）：改为老纸主题 token。
 *
 * `idle` 由 slate 灰换成纸面色卡；`running/done/warning/failed` 保留语义色相（蓝/绿/黄/红），
 * 只把边框换成墨线色系，避免半透明边框叠在白色卡片上发脏。
 */
export const TONE_CLASS: Record<ProgressTone, string> = {
  idle: "bg-paper-base text-ink-muted border-ink-line",
  running: "bg-accent-soft text-accent-seal border-accent-seal",
  done: "bg-success-soft text-success-text border-success-line",
  warning: "bg-warning-soft text-warning-text border-warning-line",
  failed: "bg-error-soft text-error-text border-error-line",
};
