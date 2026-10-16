/** 类型 / 结果徽标。颜色只用来区分，文字一律用主文本色，保证任何主题下都读得清。 */

import { KIND_LABEL, type Kind, type RowState } from "./model";

const KIND_DOT: Record<Kind, string> = {
  init: "bg-ink-subtle",
  search: "bg-accent-seal",
  ask: "bg-brand-gold",
};

export function KindBadge({ kind }: { kind: Kind }) {
  return (
    <span className="inline-flex items-center gap-1.5 whitespace-nowrap border border-ink-line bg-paper-base px-2 py-0.5 text-xs font-medium text-ink-primary">
      <span className={`h-2 w-2 rounded-full ${KIND_DOT[kind]}`} aria-hidden />
      {KIND_LABEL[kind]}
    </span>
  );
}

const STATE: Record<RowState, [string, string]> = {
  ok: ["成功", "bg-success-soft text-success-text border-success-line"],
  failed: ["失败", "bg-error-soft text-error-text border-error-line"],
  insufficient: ["证据不足", "bg-warning-soft text-warning-text border-warning-line"],
  degraded: ["降级", "bg-warning-soft text-warning-text border-warning-line"],
};

export function StateBadge({ state }: { state: RowState }) {
  const [label, cls] = STATE[state];
  return (
    <span className={`inline-flex whitespace-nowrap border px-2 py-0.5 text-xs font-medium ${cls}`}>
      {label}
    </span>
  );
}
