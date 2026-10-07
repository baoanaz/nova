/**
 * 接入指南：安装 npm 包、配置 Agent，以及可选的提示词增强。
 *
 * 设计口径（用户 2026-09-13 指定）：
 * - 卡牌一：一条 `npm install -g nova-client`；使用者是工程师，不写 Node 版本等新手前置；
 * - 卡牌二：Codex / Claude / pi 三按键，配置必定带 `--token`（缺省为占位符）；
 *   服务地址默认使用已配置的 API 基址或当前站点地址，用户可修改；
 * - 不自动带入真实 Key（避免截图/录屏泄露）；用户填了就实时替换占位符；
 * - 事实来源：`npm/README.md` / `npm/package.json` / `client/src/main.rs` 的 clap 定义。
 *
 * TASK-100 §需求6（用户 2026-09-14）：删掉页面上的解释性文案——用户是工程师，
 * 页面只留"做什么"与可粘贴的片段。
 */

import { useMemo, useState } from "react";

import { API_BASE } from "../api/client";
import {
  AGENT_PROMPT,
  AGENT_TARGETS,
  BASE_URL_PLACEHOLDER,
  INSTALL_COMMAND,
  type AgentId,
  snippetFor,
  TOKEN_PLACEHOLDER,
} from "../app/connect-info";
import { Card, CopyButton, Page } from "../components/ui";

export function ConnectPage() {
  const [target, setTarget] = useState<AgentId>("codex");
  const [baseUrl, setBaseUrl] = useState(() =>
    new URL(API_BASE || "/", window.location.origin).href.replace(/\/$/, ""),
  );
  const [token, setToken] = useState("");
  // 空串交给 `connect-info` 回落成占位符（页面不做第二套判断）。
  const ctx = useMemo(() => ({ baseUrl, token }), [baseUrl, token]);
  const snippet = useMemo(() => snippetFor(target, ctx), [target, ctx]);
  const active = AGENT_TARGETS.find((item) => item.id === target) ?? AGENT_TARGETS[0]!;

  return (
    <Page>
      <div>
        <h1 className="text-lg font-semibold">接入指南</h1>
        <p className="mt-1 text-sm text-ink-muted">安装 npm 包、配置 Agent，再按需添加提示词增强。</p>
      </div>

      <Card title="1. 下载 nova-client npm 包" actions={<CopyButton text={INSTALL_COMMAND} />}>
        <pre className="overflow-x-auto rounded bg-ink-primary p-3 font-mono text-xs text-paper-base">
          {INSTALL_COMMAND}
        </pre>
      </Card>

      <Card title="2. 配置 MCP 接入">
        <div className="mb-3 grid grid-cols-1 gap-3 sm:grid-cols-2">
          <label className="text-sm">
            <span className="mb-1 block text-xs text-ink-muted">
              服务地址（已默认填写，可修改）
            </span>
            <input
              value={baseUrl}
              onChange={(event) => setBaseUrl(event.target.value)}
              placeholder={BASE_URL_PLACEHOLDER}
              className="w-full rounded border border-ink-line bg-paper-card px-3 py-1.5 font-mono text-sm text-ink-primary"
            />
          </label>
          <label className="text-sm">
            <span className="mb-1 block text-xs text-ink-muted">
              API Key（留空则用占位符 {TOKEN_PLACEHOLDER}）
            </span>
            <input
              value={token}
              onChange={(event) => setToken(event.target.value)}
              placeholder="nova_..."
              className="w-full rounded border border-ink-line bg-paper-card px-3 py-1.5 font-mono text-sm text-ink-primary"
            />
          </label>
        </div>

        <div className="mb-3 flex flex-wrap gap-2">
          {AGENT_TARGETS.map((item) => (
            <button
              key={item.id}
              type="button"
              onClick={() => setTarget(item.id)}
              className={`rounded border px-4 py-2 text-sm ${
                target === item.id
                  ? "border-accent-seal bg-accent-seal text-accent-contrast"
                  : "border-ink-line bg-paper-card text-ink-primary hover:bg-paper-base"
              }`}
            >
              {item.label}
            </button>
          ))}
        </div>

        <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
          <span className="text-xs text-ink-muted">
            写入位置：<code className="rounded bg-paper-base px-1 text-ink-primary">{active.where}</code>
          </span>
          <CopyButton text={snippet} label="复制配置" />
        </div>
        <pre className="overflow-x-auto rounded bg-ink-primary p-3 font-mono text-xs text-paper-base">
          {snippet}
        </pre>
      </Card>

      <Card title="3. 提示词增强（可选）" actions={<CopyButton text={AGENT_PROMPT} label="复制提示词" />}>
        <p className="mb-3 text-sm text-ink-muted">
          将下面的样例加入项目的 <code className="text-ink-primary">AGENTS.md</code> 或类似的 Agent 指令文件。
        </p>
        <pre className="whitespace-pre-wrap break-words border border-ink-line bg-paper-raised p-4 font-mono text-sm leading-7 text-ink-primary">
          {AGENT_PROMPT}
        </pre>
      </Card>
    </Page>
  );
}
