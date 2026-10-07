/**
 * API Key 管理（TASK-071；TASK-110 §1.7；2026-09-15 按用户要求改为弹窗式）。
 *
 * 版式（用户原话："这个页面只有现有的 Key 卡片，然后点击卡片右上角的创建 Key，出现一个弹窗"）：
 *
 * | 区域 | 内容 |
 * |---|---|
 * | 页面 | **只有** Key 列表卡片，右上角一个「创建 Key」 |
 * | 弹窗第一行 | 名称 |
 * | 弹窗第二行 | 自定义 Key（**仅拓荒者/管理员可见**） |
 *
 * 三条安全口径（Module/06 §2.2）：
 * 1. **明文只显示一次**（用户 2026-09-15 确认保留此纪律）：创建成功后在弹窗里展示并
 *    提供复制，关掉即再取不到。列表接口**永不含明文与哈希**，页面也不缓存到 storage；
 * 2. **无特权的用户看不到自定义行**（用户要求"普通用户看不见这个 key 行"）——
 *    与旧版的"显示一行说明"不同，这次是**真的不渲染**：他点创建就得到一个随机 Key；
 * 3. 判断依据为后端 ``capabilities.canCustomKey``，**不在这里写 ``role === "beta"``**：
 *    两处各写一份角色判断必然漂移，而漂移的表现是"页面说能自定义、点了却 403"。
 */

import { useCallback, useEffect, useState } from "react";

import {
  type Account,
  type ApiKeyCreated,
  type ApiKeySummary,
  createApiKey,
  getMe,
  listApiKeys,
  revokeApiKey,
} from "../api/client";
import {
  CopyButton,
  EmptyState,
  ErrorBlock,
  LoadingBlock,
  Modal,
  Page,
} from "../components/ui";
import { formatTime } from "./DashboardPage";

/** 自定义 Key 的固定前缀（用户要求"必须以 nova_ 开头"）。 */
const KEY_PREFIX = "nova_";

export function ApiKeysPage() {
  const [keys, setKeys] = useState<ApiKeySummary[] | null>(null);
  const [account, setAccount] = useState<Account | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [creating, setCreating] = useState(false);

  const load = useCallback(async () => {
    setError(null);
    try {
      const [listed, me] = await Promise.all([listApiKeys(), getMe().catch(() => null)]);
      setKeys(listed);
      setAccount(me);
    } catch (err) {
      setKeys(null);
      setError(err);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function onRevoke(key: ApiKeySummary) {
    const confirmed = window.confirm(
      `确认撤销 API Key「${key.name || key.prefix}」？\n\n撤销后使用该 Key 的客户端会立即失效（软删）。`,
    );
    if (!confirmed) return;
    try {
      await revokeApiKey(key.id);
      await load();
    } catch (err) {
      setError(err);
    }
  }

  return (
    <Page>
      <div>
        <h1 className="text-lg font-semibold">API Key</h1>
        <p className="mt-1 text-sm text-ink-muted">
          供编辑器 / CLI / 客户端使用。明文只在创建时显示一次。
        </p>
      </div>

      {error !== null && <ErrorBlock error={error} />}

      <section className="rounded-lg border border-ink-line bg-paper-card shadow-sm">
        <header className="flex items-center justify-between gap-3 border-b border-ink-line/70 px-4 py-3">
          <h2 className="text-sm font-semibold text-ink-primary">现有的 Key</h2>
          <CreateKeyTrigger account={account} onClick={() => setCreating(true)} />
        </header>
        {keys === null ? (
          <LoadingBlock />
        ) : keys.length === 0 ? (
          <EmptyState
            title="还没有 API Key"
            hint="点右上角「创建 Key」，再把完整 Key 配置到编辑器 / CLI 客户端里；只有创建那一刻能看到完整值。"
          />
        ) : (
          <table className="w-full border-collapse text-sm">
            <thead>
              <tr className="text-left text-xs text-ink-muted">
                <th className="px-4 py-2 font-normal">名称</th>
                <th className="px-4 py-2 font-normal">Key</th>
                <th className="px-4 py-2 font-normal">来源</th>
                <th className="px-4 py-2 font-normal">创建时间</th>
                <th className="px-4 py-2 font-normal">最近使用</th>
                <th className="px-4 py-2 font-normal"></th>
              </tr>
            </thead>
            <tbody>
              {keys.map((key) => (
                <tr key={key.id} className="border-t border-ink-line/60">
                  <td className="px-4 py-2">
                    {key.name || <span className="text-ink-muted">未命名</span>}
                  </td>
                  <td className="px-4 py-2 font-mono text-xs">{key.prefix}…</td>
                  <td className="px-4 py-2 text-xs text-ink-muted">
                    {key.isCustom ? "自定义" : "随机生成"}
                  </td>
                  <td className="px-4 py-2 text-xs text-ink-muted">
                    {formatTime(key.createdAt)}
                  </td>
                  <td className="px-4 py-2 text-xs text-ink-muted">
                    {key.lastUsedAt === null ? "从未使用" : formatTime(key.lastUsedAt)}
                  </td>
                  <td className="px-4 py-2 text-right">
                    <button
                      type="button"
                      onClick={() => void onRevoke(key)}
                      className="rounded border border-error-line px-2 py-1 text-xs text-error-text hover:bg-error-soft"
                    >
                      撤销
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      {creating && (
        <CreateKeyDialog
          account={account}
          onClose={() => setCreating(false)}
          onCreated={() => void load()}
        />
      )}
    </Page>
  );
}

/**
 * 「创建 Key」按钮（列表卡片右上角）。
 *
 * 为什么把它放在 header 的 ``actions`` 位而不是列表下方：用户明确要「点卡片右上角的创建 Key」，
 * 而列表为空时它也是**唯一的入口**——放在下方会让空列表页面出现一大片空白。
 */
function CreateKeyTrigger({
  account,
  onClick,
}: {
  account: Account | null;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      // 身份还没加载完就允许点：弹窗自己会按 account 决定显示哪几行，
      // 而"等一下再点"是个没必要的限制。
      className="rounded bg-accent-seal px-3 py-1.5 text-xs font-medium text-accent-contrast hover:opacity-90"
      title={account?.capabilities.canCustomKey ? "创建随机 Key 或自定义 Key" : "创建一个 Key"}
    >
      创建 Key
    </button>
  );
}

/**
 * 创建 Key 弹窗（用户 2026-09-15 指定的版式）。
 *
 * 两行：名称（必填可选，空则未命名）+ 自定义 Key（**仅特权身份渲染**）。
 * 创建成功后**同一个弹窗**变成结果视图（展示明文 + 复制），而不是关掉再在页面上提示——
 * 用户刚点的那一下与"拿到 Key"在同一个视觉上下文里，不容易漏掉那一次展示。
 */
function CreateKeyDialog({
  account,
  onClose,
  onCreated,
}: {
  account: Account | null;
  onClose: () => void;
  onCreated: () => void;
}) {
  const [name, setName] = useState("");
  const [customKey, setCustomKey] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [created, setCreated] = useState<ApiKeyCreated | null>(null);

  const canCustomKey = account?.capabilities.canCustomKey === true;

  async function onSubmit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const result = await createApiKey(name.trim(), customKey.trim());
      setCreated(result);
      onCreated();
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  }

  if (created !== null) {
    return (
      <Modal title="Key 已创建" onClose={onClose}>
        <p className="text-sm text-ink-muted">
          这是<strong className="text-ink-primary">唯一一次</strong>能看到完整 Key 的机会，
          请立即复制到客户端配置里。
        </p>
        <div className="mt-3 flex flex-wrap items-center gap-2">
          <code className="flex-1 break-all rounded border border-ink-line bg-paper-base px-2 py-1.5 font-mono text-xs text-ink-primary">
            {created.token}
          </code>
          <CopyButton text={created.token} label="复制 Key" />
        </div>
        <div className="mt-4 flex justify-end">
          <button
            type="button"
            onClick={onClose}
            className="rounded bg-accent-seal px-3 py-1.5 text-sm font-medium text-accent-contrast hover:opacity-90"
          >
            我已保存，关闭
          </button>
        </div>
      </Modal>
    );
  }

  return (
    <Modal title="创建 API Key" onClose={busy ? undefined : onClose}>
      <form onSubmit={onSubmit} className="space-y-3">
        <label className="block text-sm">
          <span className="mb-1 block text-xs text-ink-muted">名称（便于分辨用途）</span>
          <input
            value={name}
            onChange={(event) => setName(event.target.value)}
            placeholder="例如：家里的 Cursor"
            autoFocus
            className="w-full rounded border border-ink-line bg-paper-card px-3 py-1.5 text-sm text-ink-primary"
          />
        </label>

        {canCustomKey && (
          <label className="block text-sm">
            <span className="mb-1 flex flex-wrap items-center gap-x-2 text-xs">
              <span className="rounded bg-warning-soft px-1.5 py-0.5 font-medium text-warning-text">
                🧭 {account?.title}特权
              </span>
              <span className="text-ink-muted">可自定义 Key，必须以 {KEY_PREFIX} 开头</span>
            </span>
            <input
              value={customKey}
              onChange={(event) => setCustomKey(event.target.value)}
              placeholder={`${KEY_PREFIX}my-laptop-key-2026`}
              autoComplete="off"
              className="w-full rounded border border-ink-line bg-paper-card px-3 py-1.5 font-mono text-xs text-ink-primary"
            />
            <span className="mt-1 block text-[11px] text-ink-muted">
              留空 = 服务端随机生成（{KEY_PREFIX} + 16 位字母数字）。
            </span>
          </label>
        )}

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
            type="submit"
            disabled={busy}
            className="rounded bg-accent-seal px-3 py-1.5 text-sm font-medium text-accent-contrast hover:opacity-90 disabled:opacity-40"
          >
            {busy ? "创建中…" : "创建"}
          </button>
        </div>
      </form>
    </Modal>
  );
}
