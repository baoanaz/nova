/**
 * 服务模型信息卡（TASK-100 §需求9）：LLM 与 Embedding 的**最小必要信息**。
 *
 * 从设置页移来：这两组值是"当前生效的配置"，属于用户该在首屏知道的运行时事实，
 * 而不是需要点进设置页才能看到的运维细节。
 *
 * 展示口径（用户 2026-09-14："尽量精简展示"）：
 * - LLM：模型名、厂商、最大上下文；
 * - Embedding：模型名、厂商、维度、TPM、RPM；
 * - **不展示**：baseUrl（内部拓扑）、timeout/maxTokens/temperature（调优细节）、
 *   apiKey 任何部分（服务端本就不返回）。
 *
 * 字段容错：后端注册表没有可靠数据的字段显示 `—`，不凭空编造配额。
 *
 * 独立的加载与失败处理：**本组件失败不影响控制台主体**（账户资料与工具调用是主数据）。
 * 读不到配置时安静地说明原因，不把整个控制台换成错误页。
 */

import { useEffect, useState, type ReactNode } from "react";

import { type DeploymentMeta, getMeta } from "../api/client";
import { Icon } from "./Icon";

export function ServiceModels() {
  const [meta, setMeta] = useState<DeploymentMeta | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    void (async () => {
      try {
        setMeta(await getMeta());
      } catch {
        setFailed(true);
      }
    })();
  }, []);

  return (
    <section
      className="dashboard-panel min-w-0 border-2 border-ink-line bg-paper-card p-4 shadow-sm"
      data-testid="service-models"
    >
      <h2 className="panel-heading mb-5 text-base font-semibold text-ink-primary"><Icon name="cpu" />服务模型</h2>

      {failed && <p className="text-xs text-ink-muted">读不到配置（服务不可达）。</p>}
      {!failed && meta === null && <p className="text-xs text-ink-muted">读取中…</p>}

      {meta !== null && (
        <div className="grid gap-3 sm:grid-cols-2">
          <ModelGroup
            heading="LLM"
            configured={meta.config.llm.configured}
            items={[
              ["模型", codeOrDash(meta.config.llm.model)],
              ["厂商", textOrDash(meta.config.llm.provider)],
              ["最大上下文", tokensOrDash(meta.config.llm.maxContextTokens)],
            ]}
          />
          <ModelGroup
            heading="Embedding"
            configured={meta.config.embedding.configured}
            items={[
              ["模型", codeOrDash(meta.config.embedding.model)],
              ["厂商", textOrDash(meta.config.embedding.provider)],
              ["维度", textOrDash(meta.config.embedding.dim)],
              ["TPM", textOrDash(meta.config.embedding.tpm)],
              ["RPM", textOrDash(meta.config.embedding.rpm)],
            ]}
          />
        </div>
      )}
    </section>
  );
}

function ModelGroup({
  heading,
  configured,
  items,
}: {
  heading: string;
  configured: boolean | undefined;
  items: [string, ReactNode][];
}) {
  return (
    <div className="border border-ink-line/70 bg-paper-raised p-3">
      <div className="mb-2 flex items-baseline gap-2">
        <span className="text-xs font-medium text-ink-muted">{heading}</span>
        {configured === false && (
          <span className="text-xs text-warning-text">未配置</span>
        )}
      </div>
      <dl>
        {items.map(([key, value]) => (
          <div key={key} className="flex items-baseline justify-between gap-3 border-b border-dashed border-ink-line/60 py-1.5 last:border-b-0">
            <dt className="shrink-0 text-xs text-ink-muted">{key}</dt>
            <dd className="min-w-0 break-all text-right text-xs text-ink-primary">{value}</dd>
          </div>
        ))}
      </dl>
    </div>
  );
}

/** 缺失统一显示 `—`（**不是** `null`/`undefined` 的字面量）。 */
function dash<T>(value: T | null | undefined): T | undefined {
  return value === null || value === undefined || value === "" ? undefined : value;
}

function textOrDash(value: string | number | null | undefined): ReactNode {
  const v = dash(value);
  return v === undefined ? <span className="text-ink-muted">—</span> : v;
}

function codeOrDash(value: string | null | undefined): ReactNode {
  const v = dash(value);
  if (v === undefined) return <span className="text-ink-muted">—</span>;
  return <code className="text-xs">{v}</code>;
}

function tokensOrDash(value: number | null | undefined): ReactNode {
  const v = dash(value);
  if (v === undefined) return <span className="text-ink-muted">—</span>;
  // 大数字加千位分隔（maxContext 动辄 128000，裸数字读起来费力）。
  return typeof v === "number" ? `${v.toLocaleString()} token` : String(v);
}
