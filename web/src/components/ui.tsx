/** 通用展示组件：徽标 / 卡片 / 错误块 / 复制按钮（无请求，纯展示）。 */

import { useEffect, useRef, useState, type ReactNode } from "react";

import { errorHint } from "../api/client";
import type { ProgressTone } from "./progress";
import { TONE_CLASS } from "./progress";

export function Badge({
  children,
  tone = "idle",
  title,
}: {
  children: ReactNode;
  tone?: ProgressTone;
  title?: string;
}) {
  return (
    <span
      title={title}
      className={`inline-flex items-center rounded border px-2 py-0.5 text-xs font-medium ${TONE_CLASS[tone]}`}
    >
      {children}
    </span>
  );
}

export function Card({
  title,
  actions,
  children,
}: {
  title?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
}) {
  return (
    <section className="min-w-0 border-2 border-ink-line bg-paper-card shadow-sm">
      {(title || actions) && (
        <header className="flex flex-wrap items-center justify-between gap-3 border-b border-ink-line bg-paper-raised px-5 py-4">
          <h2 className="text-base font-semibold text-ink-primary">{title}</h2>
          {actions}
        </header>
      )}
      <div className="px-5 py-5">{children}</div>
    </section>
  );
}

/** 把任意抛出物转成可读文本：**永不出 `[object Object]`**（TASK-083 要求）。 */
function describeError(error: unknown): string {
  if (error instanceof Error) return error.message;
  if (typeof error === "string") return error;
  if (typeof error === "object" && error !== null && "message" in error) {
    const message = (error as { message: unknown }).message;
    if (typeof message === "string" && message.length > 0) return message;
  }
  // null/undefined 与无法序列化的值不能直接落进兜底文案（`String(null)` 会渲染成 "null"）。
  if (error !== null && error !== undefined) {
    try {
      const json = JSON.stringify(error);
      if (json && json !== "{}" && json !== "null") return json;
    } catch {
      // 循环引用等无法序列化的情况：落到下面的兜底文案。
    }
  }
  return "请求失败（错误对象没有可读的 message）";
}

/** 错误块：服务端 message + `code` + 由 `code` 推出的下一步指引（不各页各写文案）。 */
export function ErrorBlock({ error }: { error: unknown }) {
  const message = describeError(error);
  const code =
    typeof error === "object" && error !== null && "code" in error
      ? String((error as { code: unknown }).code)
      : null;
  const hint = errorHint(error);

  return (
    <div className="rounded border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-800">
      <div className="flex flex-wrap items-baseline gap-2">
        <span className="font-medium">{message}</span>
        {code && <code className="rounded bg-rose-100 px-1 text-xs">{code}</code>}
      </div>
      {hint && hint !== message && <p className="mt-1 text-rose-700">{hint}</p>}
    </div>
  );
}

/** HTTP 公网预览没有 Clipboard API，使用用户点击触发的兼容复制。 */
function copyWithSelection(text: string): boolean {
  const previousFocus = document.activeElement;
  const field = document.createElement("textarea");
  field.value = text;
  field.readOnly = true;
  field.style.cssText = "position:fixed;left:-9999px;top:0;font-size:16px";
  document.body.appendChild(field);
  try {
    field.focus({ preventScroll: true });
    field.select();
    return document.execCommand("copy");
  } catch {
    return false;
  } finally {
    field.remove();
    if (previousFocus instanceof HTMLElement) previousFocus.focus({ preventScroll: true });
  }
}

export function CopyButton({ text, label = "复制" }: { text: string; label?: string }) {
  const [status, setStatus] = useState<"idle" | "copied" | "failed">("idle");
  const resetTimer = useRef<number>();

  useEffect(() => () => window.clearTimeout(resetTimer.current), []);

  async function onCopy() {
    let copied = false;
    if (navigator.clipboard?.writeText) {
      try {
        await navigator.clipboard.writeText(text);
        copied = true;
      } catch {
        // 浏览器拒绝 Clipboard API 时继续尝试兼容复制。
      }
    }
    if (!copied) copied = copyWithSelection(text);
    setStatus(copied ? "copied" : "failed");
    window.clearTimeout(resetTimer.current);
    resetTimer.current = window.setTimeout(() => setStatus("idle"), 2000);
  }

  return (
    <button
      type="button"
      onClick={onCopy}
      aria-live="polite"
      className="rounded border border-ink-line bg-paper-card px-2 py-1 text-xs text-ink-primary hover:bg-paper-base"
    >
      {status === "copied" ? "已复制" : status === "failed" ? "请手动复制" : label}
    </button>
  );
}

export function KeyValue({ items }: { items: [string, ReactNode][] }) {
  return (
    <dl className="grid grid-cols-1 gap-x-6 gap-y-2 sm:grid-cols-2">
      {items.map(([key, value]) => (
        <div key={key} className="flex items-baseline justify-between gap-3 border-b border-dashed border-ink-line/70 pb-1">
          <dt className="text-xs text-ink-muted">{key}</dt>
          <dd className="text-sm text-ink-primary">{value}</dd>
        </div>
      ))}
    </dl>
  );
}

export function LoadingBlock({ text = "加载中…" }: { text?: string }) {
  return <p className="py-6 text-center text-sm text-ink-muted">{text}</p>;
}

/**
 * 页面宽度容器（TASK-100）。
 *
 * 壳层（`Layout`）不再限定宽度，由页面自己选：
 * - `Page`（**默认**）：`max-w-5xl`（64rem）——控制台/项目/接入指南/API Key/设置；
 *   表单与信息卡在过宽的屏幕上会拉得很长、阅读困难。
 * - `WidePage`：`max-w-[120rem]`——**只有历史记录**这种列多的宽表需要。
 *
 * 为什么不用“按页面类型自动判断”：用户明确说“只有历史记录调宽即可”，
 * 自动判断会在新页面出现时默默给出错误宽度；显式声明让每个页面自己负责。
 */
export function Page({ children }: { children: ReactNode }) {
  return <div className="page-content mx-auto max-w-5xl">{children}</div>;
}

export function WidePage({ children }: { children: ReactNode }) {
  return <div className="page-content mx-auto max-w-[120rem]">{children}</div>;
}

/**
 */
export function Switch({
  checked,
  onChange,
  label,
  disabled = false,
  testId,
}: {
  checked: boolean;
  onChange: (next: boolean) => void;
  label: string;
  disabled?: boolean;
  testId?: string;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      disabled={disabled}
      data-testid={testId}
      onClick={() => onChange(!checked)}
      className={`relative inline-flex h-4 w-8 shrink-0 items-center rounded-full border transition-colors disabled:opacity-40 ${
        checked ? "border-accent-seal bg-accent-seal" : "border-ink-line bg-paper-base"
      }`}
    >
      <span
        aria-hidden="true"
        className={`absolute h-3 w-3 rounded-full bg-paper-card shadow-sm transition-transform ${
          checked ? "translate-x-4" : "translate-x-0.5"
        }`}
      />
    </button>
  );
}

/**
 * 二次确认弹窗（TASK-094 §D）：破坏性操作前必须让用户明确看过后果。
 *
 * 为什么用原生 `<dialog>` 而不是自造遮罩层：浏览器已经给了焦点陷阱、`Esc` 关闭与
 * `aria-modal` 语义，自造一套的常见后果是“键盘用户被卡在遮罩里”与“Esc 不生效”。
 *
 * 确认按钮使用独立的 danger token，保持危险操作的辨识度。
 *
 * 调用方负责挂载/卸载本组件（`open` 控制）；**取消按钮不发任何请求**——那是调用方必须
 * 自己保证的（本组件只回调 `onCancel`/`onConfirm`，不做业务判断）。
 */
export function ConfirmDialog({
  open,
  title,
  confirmLabel = "确认",
  cancelLabel = "取消",
  busy = false,
  onConfirm,
  onCancel,
  children,
}: {
  open: boolean;
  title: ReactNode;
  confirmLabel?: string;
  cancelLabel?: string;
  busy?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
  children: ReactNode;
}) {
  const ref = useRef<HTMLDialogElement | null>(null);

  useEffect(() => {
    const dialog = ref.current;
    if (dialog === null) return;
    if (open && !dialog.open) dialog.showModal();
    if (!open && dialog.open) dialog.close();
  }, [open]);

  return (
    <dialog
      ref={ref}
      // Esc 关闭也要走 onCancel（否则调用方以为弹窗还开着，状态不同步）。
      onCancel={(event) => {
        event.preventDefault();
        if (!busy) onCancel();
      }}
      className="w-[calc(100%-2rem)] max-w-lg border-2 border-ink-line bg-paper-card p-0 shadow-xl backdrop:bg-ink-primary/40"
    >
      <div className="p-4">
        <h2 className="text-sm font-semibold text-ink-primary">{title}</h2>
        <div className="mt-2 space-y-2 text-sm text-ink-muted">{children}</div>
        <div className="mt-4 flex justify-end gap-2">
          <button
            type="button"
            onClick={onCancel}
            disabled={busy}
            className="rounded border border-ink-line px-3 py-1.5 text-sm text-ink-primary hover:bg-paper-raised disabled:opacity-50"
          >
            {cancelLabel}
          </button>
          <button
            type="button"
            onClick={onConfirm}
            disabled={busy}
            className="rounded bg-danger-base px-3 py-1.5 text-sm font-medium text-white hover:bg-danger-hover disabled:opacity-50"
          >
            {busy ? "处理中…" : confirmLabel}
          </button>
        </div>
      </div>
    </dialog>
  );
}

/**
 * 通用弹窗（TASK-110）：标题 + 任意内容，点遮罩/按 Esc/点叉都能关。
 *
 * 为什么不用原生 ``<dialog>``（``ConfirmDialog`` 用了）：那个的 API 适合“确认/取消”这类
 * 固定动作，而本组件承载的是**任意表单**（如创建 Key），需要自由控制底部按钮与
 * “提交中不许关”的行为。两个组件共存是有意的：简单确认继续用 ``ConfirmDialog``。
 *
 * ``onClose`` 传 ``undefined`` 时**禁止关闭**（提交中）——此时遮罩点击与 Esc 都不生效，
 * 但**保留叉号并置灰**（而不是让它消失：突然少一个东西比一个禁用的按钮更令人不安）。
 */
export function Modal({
  title,
  onClose,
  children,
}: {
  title: ReactNode;
  onClose?: () => void;
  children: ReactNode;
}) {
  // Esc 关闭：提交中（onClose 未定义）时不响应。
  useEffect(() => {
    if (!onClose) return;
    function onKey(event: KeyboardEvent) {
      if (event.key === "Escape") onClose?.();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div
      // 遮罩：点它关闭。用 button 而不是 div，键盘也能关（与 Layout 的抽屉遮罩同一手法）。
      className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-ink-primary/40 p-4 pt-16"
    >
      <button
        type="button"
        aria-label="关闭弹窗"
        onClick={() => onClose?.()}
        disabled={!onClose}
        className="absolute inset-0 cursor-default"
      />
      <div
        role="dialog"
        aria-modal="true"
        aria-label={typeof title === "string" ? title : undefined}
        className="relative z-10 w-full max-w-md border-2 border-ink-line bg-paper-card p-6 shadow-xl"
      >
        <div className="mb-3 flex items-start justify-between gap-3">
          <h2 className="text-sm font-semibold text-ink-primary">{title}</h2>
          <button
            type="button"
            aria-label="关闭"
            onClick={() => onClose?.()}
            disabled={!onClose}
            className="rounded px-1.5 text-ink-muted hover:bg-paper-base hover:text-ink-primary disabled:opacity-40"
          >
            ✕
          </button>
        </div>
        {children}
      </div>
    </div>
  );
}

/**
 * 空态块（TASK-083）：**只用于"请求成功但没有数据"**。
 *
 * 为什么要有这个组件："0 条"与"后端坏了"在旧版看起来一样——用户分不清
 * 是"自己还没用"还是"服务没记上"。因此本组件把两件事钉死：
 *
 * 1. **`hint` 必须说明"怎样才会有数据"**（可操作的一句话），不许写"暂无数据"；
 * 2. **错误一律走 `ErrorBlock`**——请求失败时禁止渲染本组件，否则"后端坏了"会被
 *    伪装成"还没数据"。调用方必须先判错误、再判空数组（各页先渲染 `ErrorBlock`，
 *    确认无错误后才轮到 `EmptyState`）。
 */
export function EmptyState({
  title,
  hint,
  action,
}: {
  title: ReactNode;
  hint?: ReactNode;
  action?: ReactNode;
}) {
  return (
    <div className="px-4 py-10 text-center">
      <p className="text-sm font-medium text-ink-primary">{title}</p>
      {hint && <p className="mx-auto mt-1 max-w-prose text-sm text-ink-muted">{hint}</p>}
      {action && <div className="mt-3 flex justify-center">{action}</div>}
    </div>
  );
}
