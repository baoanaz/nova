/**
 * 设置（TASK-088 §F；TASK-100 §需求9/10 精简为**只留 LLM**）。
 *
 * 用户 2026-09-14 原话："设置页面，其中 LLM 修改成自定义的配置方式，模型名、URL、Key。
 * 目的是给用户自定义 LLM 的选择。设置页面只保留 LLM 的配置，存储、部署形态等其他功能删除，精简。"
 *
 * 因此本页只做一件事：**让用户配自己的总结模型**。
 * - 存储配额 → 移到 `/projects`（那里有进度条与删除入口，是它的使用场景）；
 * - 部署形态（版本/本地模式/需要登录/开放注册）→ 删除（用户不需要知道这些）；
 * - 向量模型信息 → 移到控制台的「服务模型」卡（与 LLM 并排展示当前生效值）。
 *
 * **保存能力已实现**（TASK-099 §C 后端已落地）：`PUT /api/auth/llm-config` 写入用户配置，
 * `DELETE` 删除后回落服务端默认。本页：
 * - 顶部如实显示**当前实际生效**的那一份（`config.llm.source`：`user` = 你自己配的，
 *   `server` = 服务端环境变量）；
 * - 三个字段 + 保存/清除；保存后重新拉取 `/api/meta` 以刷新"当前值"；
 *
 * 安全口径（承 TASK-088，不放松）：
 * - **不显示 key 的任何部分**（含前缀/长度）——服务端连长度都不返回；
 * - 页面不把 Key 写进 localStorage/sessionStorage，提交后即从组件状态清除。
 */

import { useEffect, useState } from "react";

import {
  type DeploymentMeta,
  type LlmProtocol,
  type LlmTestResult,
  deleteUserLlmConfig,
  errorHint,
  getMeta,
  saveUserLlmConfig,
  testUserLlmConfig,
} from "../api/client";
import { Card, ErrorBlock, LoadingBlock, Page } from "../components/ui";

/**
 * 协议下拉框的选项（TASK-113 / D-47）。
 *
 * **为什么在前端写一份而不是全从 `/api/meta` 取**：`label` 是 UI 文案（中文说明 + 端点），
 * 服务端只给机器名（`openai`/`responses`/`anthropic`）与“支持哪些”列表。这样新增协议时
 * 服务端与前端各改一处，而不是让服务端返回界面文案。
 *
 * `hint` 里写清每种协议打哪个端点：用户选协议时真正要对照的就是“我的网关支持哪个端点”。
 */
const PROTOCOL_OPTIONS: ReadonlyArray<{
  value: LlmProtocol;
  label: string;
  hint: string;
}> = [
  {
    value: "openai",
    label: "OpenAI Chat Completions",
    hint: "/v1/chat/completions（最常见；默认）",
  },
  {
    value: "responses",
    label: "OpenAI Responses",
    hint: "/v1/responses（较新的 OpenAI 协议；DeepSeek 新模型常用）",
  },
  {
    value: "anthropic",
    label: "Anthropic Messages",
    hint: "/v1/messages（Claude 家族与部分中转网关）",
  },
];

export function SettingsPage() {
  const [meta, setMeta] = useState<DeploymentMeta | null>(null);
  const [error, setError] = useState<unknown>(null);
  //: 保存/清除的反馈（成功文案或错误）；null = 还没有操作过。
  const [notice, setNotice] = useState<string | null>(null);
  const [saveError, setSaveError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);

  //: 表单状态。**不持久化**——Key 只活在内存里，提交后清除。
  const [model, setModel] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [apiKey, setApiKey] = useState("");
  //: 协议（TASK-113）：默认 openai（与升级前行为一致）。
  const [protocol, setProtocol] = useState<LlmProtocol>("openai");
  //: 自检结果（null = 还没测过）；保存/清除/改动表单后失效，避免绿灯留在旧输入上。
  const [testResult, setTestResult] = useState<LlmTestResult | null>(null);
  const [testing, setTesting] = useState(false);

  useEffect(() => {
    void (async () => {
      try {
        const resolved = await getMeta();
        setMeta(resolved);
        // 用当前生效值预填"非敏感"字段（Key 永不预填——服务端不返回它）。
        setModel(resolved.config.llm.model ?? "");
        setBaseUrl(resolved.config.llm.baseUrl ?? "");
        setProtocol(resolved.config.llm.protocol ?? "openai");
      } catch (err) {
        setError(err);
      }
    })();
  }, []);

  /** 重新拉取生效值（保存/清除后调用：否则顶部"当前"会停留在旧值上）。 */
  async function refresh() {
    const resolved = await getMeta();
    setMeta(resolved);
    setModel(resolved.config.llm.model ?? "");
    setBaseUrl(resolved.config.llm.baseUrl ?? "");
    setProtocol(resolved.config.llm.protocol ?? "openai");
  }

  async function onTest() {
    setTesting(true);
    setNotice(null);
    setSaveError(null);
    setTestResult(null);
    try {
      // L1：零成本探测（验证 key、模型名、协议声明）。
      setTestResult(
        await testUserLlmConfig({ model, baseUrl, apiKey, protocol, deep: false }),
      );
    } catch (err) {
      setSaveError(err);
    } finally {
      setTesting(false);
    }
  }

  async function onTestDeep() {
    setTesting(true);
    setNotice(null);
    setSaveError(null);
    try {
      // L2：真发一次最小请求（能测出“模型列表正常但推理失败”）。
      setTestResult(
        await testUserLlmConfig({ model, baseUrl, apiKey, protocol, deep: true }),
      );
    } catch (err) {
      setSaveError(err);
    } finally {
      setTesting(false);
    }
  }

  async function onSave() {
    setBusy(true);
    setNotice(null);
    setSaveError(null);
    try {
      await saveUserLlmConfig({ model, baseUrl, apiKey, protocol });
      // 提交后立即从组件状态清除 key（卡内安全口径：它不该在内存里多留一秒）。
      setApiKey("");
      setTestResult(null);
      await refresh();
      setNotice("已保存：之后 ask_project 会用你的配置");
    } catch (err) {
      setSaveError(err);
    } finally {
      setBusy(false);
    }
  }

  async function onClear() {
    setBusy(true);
    setNotice(null);
    setSaveError(null);
    try {
      await deleteUserLlmConfig();
      setApiKey("");
      setTestResult(null);
      await refresh();
      setNotice("已清除：之后 ask_project 回落服务端默认");
    } catch (err) {
      setSaveError(err);
    } finally {
      setBusy(false);
    }
  }

  if (error !== null) {
    return (
      <Page>
        <h1 className="text-lg font-semibold">设置</h1>
        <ErrorBlock error={error} />
      </Page>
    );
  }
  if (meta === null) return <LoadingBlock text="正在读取配置…" />;

  const { llm } = meta.config;
  const usingUser = llm.source === "user";

  return (
    <Page>
      <div>
        <h1 className="text-lg font-semibold">设置</h1>
        <p className="mt-1 text-sm text-ink-muted">
          自定义你的 LLM 总结模型：自由选择模型、接口地址和 API Key。
        </p>
      </div>

      <Card
        title="自定义 LLM 总结模型"
        actions={
          llm.configured ? (
            <span className="text-xs text-ink-muted">
              当前：{llm.model ?? "已配置"}
              {usingUser ? "（你的配置）" : "（服务端默认）"}
            </span>
          ) : (
            <span className="text-xs text-warning-text">未配置</span>
          )
        }
      >
        {usingUser && (
          <p
            data-testid="llm-source-user"
            className="mb-3 rounded border border-ink-line bg-paper-base px-3 py-2 text-xs text-ink-muted"
          >
            当前生效的是<strong>你自己的配置</strong>；清除后会回落服务端默认。
          </p>
        )}

        {notice !== null && (
          <p
            data-testid="llm-notice"
            className="mb-3 rounded border border-ink-line bg-paper-base px-3 py-2 text-xs text-ink-primary"
          >
            {notice}
          </p>
        )}
        {saveError !== null && (
          <p
            data-testid="llm-save-error"
            className="mb-3 rounded border border-warning-line px-3 py-2 text-xs text-warning-text"
          >
            {errorHint(saveError) ?? "保存失败，请稍后再试。"}
          </p>
        )}

        <form
          className="space-y-3"
          onSubmit={(event) => {
            event.preventDefault();
            void onSave();
          }}
        >
          <Field label="模型名" hint="例如 deepseek-chat / gpt-4o-mini">
            <input
              name="llmModel"
              value={model}
              onChange={(event) => {
                setModel(event.target.value);
                setTestResult(null);
              }}
              placeholder="deepseek-flash"
              className="w-full rounded border border-ink-line bg-paper-card px-3 py-2 font-mono text-sm text-ink-primary disabled:opacity-60"
            />
          </Field>

          {/**
           * 协议选择（TASK-113）：选错协议会表现为“保存成功但 ask 持续 503”，
           * 因此把它变成显式选项 + 可测试（下方的“测试连接”会回报该模型声明支持哪些协议）。
           */}
          <Field
            label="协议"
            hint={PROTOCOL_OPTIONS.find((item) => item.value === protocol)?.hint ?? ""}
          >
            <select
              name="llmProtocol"
              value={protocol}
              onChange={(event) => {
                setProtocol(event.target.value as LlmProtocol);
                setTestResult(null);
              }}
              className="w-full rounded border border-ink-line bg-paper-card px-3 py-2 text-sm text-ink-primary disabled:opacity-60"
            >
              {PROTOCOL_OPTIONS.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </Field>

          <Field
            label="接口地址"
            hint="base URL（例 https://host/v1）；不要填到 /chat/completions"
          >
            <input
              name="llmBaseUrl"
              value={baseUrl}
              onChange={(event) => {
                setBaseUrl(event.target.value);
                setTestResult(null);
              }}
              placeholder="https://api.example.com/v1"
              className="w-full rounded border border-ink-line bg-paper-card px-3 py-2 font-mono text-sm text-ink-primary disabled:opacity-60"
            />
          </Field>

          <Field label="API Key" hint="只保存在服务端，不会回显">
            <input
              name="llmApiKey"
              type="password"
              value={apiKey}
              onChange={(event) => {
                setApiKey(event.target.value);
                setTestResult(null);
              }}
              autoComplete="off"
              placeholder={llm.apiKeyConfigured ? "已配置（留空则不改）" : "sk-..."}
              className="w-full rounded border border-ink-line bg-paper-card px-3 py-2 font-mono text-sm text-ink-primary disabled:opacity-60"
            />
          </Field>

          <div className="flex flex-wrap gap-2 pt-1">
            <button
              type="submit"
              disabled={busy}
              className="rounded bg-accent-seal px-4 py-1.5 text-sm font-medium text-accent-contrast disabled:opacity-40"
            >
              保存
            </button>
            <button
              type="button"
              disabled={busy || testing}
              onClick={() => {
                void onTest();
              }}
              className="rounded border border-ink-line px-4 py-1.5 text-sm text-ink-primary hover:bg-paper-base disabled:opacity-40"
            >
              {testing ? "测试中…" : "测试连接"}
            </button>
            <button
              type="button"
              disabled={busy || testing}
              onClick={() => {
                void onTestDeep();
              }}
              className="rounded border border-ink-line px-4 py-1.5 text-sm text-ink-primary hover:bg-paper-base disabled:opacity-40"
            >
              测试连接（发真实请求）
            </button>
            <button
              type="button"
              disabled={busy || testing}
              onClick={() => {
                void onClear();
              }}
              className="rounded border border-ink-line px-4 py-1.5 text-sm text-ink-primary hover:bg-paper-base disabled:opacity-40"
            >
              清除
            </button>
          </div>
        </form>

        {testResult !== null && <TestResultPanel result={testResult} />}

        {!llm.configured && !usingUser && llm.missingEnv.length > 0 && (
          <p className="mt-3 text-xs text-warning-text">
            尚未配置总结模型。请在上方填写模型名、接口地址和 API Key；保存后
            ask_project 才会生成总结，未配置时只返回检索结果。
          </p>
        )}

        <p className="mt-3 text-xs text-ink-muted">
          提示：先点「测试连接」验证地址/Key/模型名/协议（零成本），确认通过后再保存。
          如果上游声明该模型不支持你选的协议，「测试连接」会直接给出建议协议。
        </p>
      </Card>
    </Page>
  );
}

function TestResultPanel({ result }: { result: LlmTestResult }) {
  /**
   * 自检结果面板（TASK-113）。
   *
   * 为什么要展示这么多字段而不是一个绿灯：用户真正需要知道的是**下一步该改什么**。
   * 模型未找到时列出可用模型名、协议不匹配时给出建议值，都是从“为什么失败”到“怎么修”
   * 的最短路径；而这两个场景正是实测中真实发生过的配置错误。
   */
  const tone = result.ok
    ? "border-success-line text-success-text"
    : "border-warning-line text-warning-text";
  return (
    <div
      data-testid="llm-test-result"
      className={`mt-3 rounded border px-3 py-2 text-xs ${tone}`}
    >
      <p className="font-medium" data-testid="llm-test-status">
        {result.ok ? "✓ " : "✕ "}
        {result.message}
      </p>
      <dl className="mt-2 grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5 text-ink-muted">
        <dt>协议</dt>
        <dd data-testid="llm-test-protocol">
          {result.protocolLabel ?? result.protocol}
        </dd>
        <dt>端点</dt>
        <dd className="break-all font-mono">{result.endpoint}</dd>
        {result.modelFound !== null && result.modelFound !== undefined && (
          <>
            <dt>模型</dt>
            <dd>{result.modelFound ? "已找到" : "未在上游列表中"}</dd>
          </>
        )}
        {result.supportedProtocols !== undefined && result.supportedProtocols.length > 0 && (
          <>
            <dt>上游声明</dt>
            <dd>{result.supportedProtocols.join(" / ")}</dd>
          </>
        )}
        {result.suggestedProtocol && result.protocolMismatch && (
          <>
            <dt>建议</dt>
            <dd data-testid="llm-test-suggestion">
              把协议改为 {result.suggestedProtocol}
            </dd>
          </>
        )}
        {result.detail && (
          <>
            <dt>上游报错</dt>
            <dd className="break-all font-mono">{result.detail}</dd>
          </>
        )}
      </dl>
    </div>
  );
}

function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint: string;
  children: React.ReactNode;
}) {
  return (
    <label className="block">
      <span className="mb-1 flex items-baseline justify-between gap-2">
        <span className="text-sm text-ink-primary">{label}</span>
        <span className="text-xs text-ink-muted">{hint}</span>
      </span>
      {children}
    </label>
  );
}
