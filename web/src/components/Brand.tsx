import { site } from "../../site.config";
import logoUrl from "../assets/nova-logo.webp";

/** 共用 NOVA 标志；装饰图片留空 alt，品牌名称由文字提供。 */
export function Brand({ compact = false }: { compact?: boolean }) {
  return (
    <span className="inline-flex min-w-0 items-center gap-3">
      <img className="brand-mark" src={logoUrl} alt="" width="44" height="44" />
      <span className="min-w-0">
        <span className="block break-words text-xl font-bold tracking-[0.18em] text-ink-primary">{site.name}</span>
        {!compact && <span className="mt-1 block text-[11px] leading-relaxed tracking-wide text-ink-muted">{site.tagline}</span>}
      </span>
    </span>
  );
}
