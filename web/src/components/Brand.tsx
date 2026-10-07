import { site } from "../../site.config";
const logoUrl = new URL("../assets/nova-wordmark-small.png", import.meta.url).href;

/** 控制台和移动导航共用横向字标，按原比例完整显示。 */
export function Brand({ compact = false }: { compact?: boolean }) {
  return (
    <span className="inline-flex min-w-0 flex-col items-start">
      <img className={compact ? "block h-auto w-24 max-w-full" : "block h-auto w-36 max-w-full"} src={logoUrl} alt={site.name} />
      {!compact && <span className="mt-2 block text-[11px] leading-relaxed tracking-wide text-ink-muted">{site.tagline}</span>}
    </span>
  );
}
