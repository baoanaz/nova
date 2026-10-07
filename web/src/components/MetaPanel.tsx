/**
 * `meta` 面板（TASK-070 §D）：把检索 trace 如实摊开给使用者。
 *
 * 字段全部来自 CF-05 的 `meta`（`packmeta.py` 冻结字段集），**不推导、不美化**：
 * `answerable=false` 就是 false，`degraded` 就是降级，`citationCoverage` 之类未测量的东西一律不显示假值。
 */

import type { AskResponse, PackMeta } from "../api/types";
import { Badge, KeyValue } from "./ui";
import type { ProgressTone } from "./progress";

/** 回答状态 → 徽标（`ask` 的三种 status；`degraded` 必须显著，不许包装成答案）。 */
export function statusTone(status: AskResponse["status"]): ProgressTone {
  if (status === "answered") return "done";
  if (status === "insufficient_evidence") return "warning";
  return "idle";
}

export const STATUS_LABEL: Record<AskResponse["status"], string> = {
  answered: "answered（已作答）",
  insufficient_evidence: "insufficient_evidence（证据不足）",
  degraded: "degraded（降级包，不是 LLM 答案）",
};

function confidenceTone(meta: PackMeta): ProgressTone {
  if (!meta.answerable) return "warning";
  if (meta.confidence === "high") return "done";
  return "running";
}

export function MetaPanel({ meta }: { meta: PackMeta }) {
  const freshness = describeFreshness(meta);

  return (
    <div className="space-y-4">
      <KeyValue
        items={[
          [
            "answerable",
            <Badge key="a" tone={confidenceTone(meta)}>
              {String(meta.answerable)}
            </Badge>,
          ],
          ["confidence", meta.confidence ?? "—"],
          ["mode", meta.mode],
          ["channelsUsed", meta.channelsUsed.join(", ") || "—"],
          ["candidateCount", String(meta.candidateCount)],
          ["evidence / docs / flows", `${meta.evidenceCount} / ${meta.docsCount} / ${meta.flowsCount}`],
        ]}
      />

      <KeyValue
        items={[
          ["budget.usedTokens", String(meta.budget.usedTokens)],
          ["budget.hardCap", String(meta.budget.hardCap)],
          ["budget.truncated", String(meta.budget.truncated)],
          ["budget.omittedCount", String(meta.budget.omittedCount)],
        ]}
      />

      <KeyValue
        items={[
          ["freshness.indexedAt", meta.freshness.indexedAt ? formatTime(meta.freshness.indexedAt) : "—"],
          ["freshness.staleFiles", String(meta.freshness.staleFiles.length)],
          ["freshness.indexingFiles", String(meta.freshness.indexingFiles.length)],
          ["freshness.rescanError", meta.freshness.rescanError ?? "—"],
        ]}
      />
      <p className="text-xs text-ink-muted">{freshness}</p>

      {meta.degraded && (
        <div className="rounded border border-warning-line bg-warning-soft px-3 py-2 text-xs text-warning-text">
          <p className="font-medium">degraded = true</p>
          <p className="mt-1">{meta.degradedReason ?? "未提供原因"}</p>
        </div>
      )}

      {meta.missingEvidence.length > 0 && (
        <div>
          <h3 className="mb-1 text-xs font-semibold text-ink-muted">
            missingEvidence（{meta.missingEvidence.length}）
          </h3>
          <ul className="space-y-1 text-xs text-ink-primary">
            {meta.missingEvidence.map((item, index) => (
              <li key={`${item.code}-${index}`} className="rounded bg-paper-base px-2 py-1">
                <code className="mr-2 text-ink-muted">{item.code}</code>
                {item.message}
                {item.symbol && <span className="ml-1 font-mono text-ink-muted">{item.symbol}</span>}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

/** 新鲜度的一句话（`indexedAt` 为空时不许说"fresh"）。 */
export function describeFreshness(meta: PackMeta, now: number = Date.now()): string {
  const { indexedAt, staleFiles, indexingFiles, rescanError } = meta.freshness;
  const parts: string[] = [];
  if (indexedAt) {
    const ageS = Math.max(0, Math.round(now / 1000 - indexedAt));
    parts.push(`index: fresh (${ageS}s ago)`);
  } else {
    parts.push("index: 未索引");
  }
  if (staleFiles.length > 0) parts.push(`stale ${staleFiles.length} 文件`);
  if (indexingFiles.length > 0) parts.push(`indexing ${indexingFiles.length} 文件`);
  if (rescanError) parts.push(`重扫失败：${rescanError}`);
  return parts.join(" · ");
}

export function formatTime(unixSeconds: number): string {
  return new Date(unixSeconds * 1000).toLocaleString();
}

/** 证据概览表（`ask` 的 `evidenceSummary`）。 */
export function EvidenceTable({ items }: { items: AskResponse["evidenceSummary"] }) {
  if (items.length === 0) return null;
  return (
    <table className="w-full border-collapse text-xs">
      <thead>
        <tr className="text-ink-muted">
          <th className="border-b border-ink-line px-2 py-1 text-left">id</th>
          <th className="border-b border-ink-line px-2 py-1 text-left">path</th>
          <th className="border-b border-ink-line px-2 py-1 text-left">lines</th>
          <th className="border-b border-ink-line px-2 py-1 text-left">tier</th>
          <th className="border-b border-ink-line px-2 py-1 text-left">score</th>
        </tr>
      </thead>
      <tbody>
        {items.map((item) => (
          <tr key={item.id}>
            <td className="border-b border-ink-line/60 px-2 py-1 font-mono">{item.id}</td>
            <td className="border-b border-ink-line/60 px-2 py-1 font-mono">{item.path}</td>
            <td className="border-b border-ink-line/60 px-2 py-1 font-mono">
              {item.lines ? `${item.lines[0]}-${item.lines[1]}` : "—"}
            </td>
            <td className="border-b border-ink-line/60 px-2 py-1">{item.tier ?? "—"}</td>
            <td className="border-b border-ink-line/60 px-2 py-1">
              {item.score === null ? "—" : item.score.toFixed(4)}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
