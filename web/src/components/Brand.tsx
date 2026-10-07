import { site } from "../../site.config";
import { Icon } from "./Icon";

/** 通用代码符号不绑定品牌首字母，换名时无需替换图标。 */
export function Brand({ compact = false }: { compact?: boolean }) {
  return (
    <span className="inline-flex min-w-0 items-center gap-3">
      <span className="brand-mark" aria-hidden="true"><Icon name="connect" className="h-5 w-5" /></span>
      <span className="min-w-0">
        <span className="block break-words text-xl font-bold tracking-tight text-ink-primary">{site.name}</span>
        {!compact && <span className="mt-1 block text-[10px] leading-relaxed tracking-wide text-ink-muted">{site.tagline}</span>}
      </span>
    </span>
  );
}
