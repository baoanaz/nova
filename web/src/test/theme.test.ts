import { readFileSync } from "node:fs";
import postcss from "postcss";
import tailwindcss from "tailwindcss";
import { describe, expect, it } from "vitest";

const css = readFileSync("src/index.css", "utf8");
const loginCss = readFileSync("src/pages/LoginPage.css", "utf8");
const config = readFileSync("tailwind.config.js", "utf8");
const tokens = new Map<string, number[]>();
postcss.parse(css).walkRules(":root", (rule) => {
  rule.walkDecls((decl) => {
    if (/^\d+ \d+ \d+$/.test(decl.value)) {
      tokens.set(decl.prop.replace("--nova-", ""), decl.value.split(" ").map(Number));
    }
  });
});

function rgb(name: string): [number, number, number] {
  const value = tokens.get(name);
  if (!value || value.length !== 3) throw new Error(`Missing color token: ${name}`);
  return value as [number, number, number];
}

function luminance(name: string): number {
  return rgb(name).reduce((sum, channel, i) => {
    const s = channel / 255;
    const linear = s <= 0.04045 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
    return sum + linear * ([0.2126, 0.7152, 0.0722][i] ?? 0);
  }, 0);
}

function contrast(foreground: string, background: string): number {
  const a = luminance(foreground);
  const b = luminance(background);
  return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
}

describe("Warm Minimalism design tokens", () => {
  it.each([
    ["bg", "FAF9F5"], ["surface", "FFFFFF"], ["surface-secondary", "F5F4ED"],
    ["surface-warm", "F0EEE6"], ["brand", "D97757"], ["hero", "EBC9B7"],
    ["selected", "FBE7DA"], ["text", "141413"], ["text-secondary", "5E5D59"],
    ["text-subtle", "B0AEA5"], ["border", "E8E6DC"], ["success", "788C5D"],
    ["action", "292522"], ["action-hover", "403934"], ["on-action", "FFFFFF"],
  ])("%s matches #%s", (name, hex) => {
    expect(rgb(name).map((v) => v.toString(16).padStart(2, "0")).join("").toUpperCase()).toBe(hex);
  });

  it.each([
    ["text", "bg"], ["text", "surface"], ["text-secondary", "bg"],
    ["text-secondary", "surface-secondary"], ["text", "hero"],
    ["accent-text", "selected"], ["accent-text", "bg"],
    ["on-action", "action"], ["on-action", "action-hover"], ["on-action", "action-active"],
    ["on-action", "accent-control"], ["on-action", "danger"], ["on-action", "danger-hover"],
    ["success-text", "success-soft"], ["warning-text", "warning-soft"], ["error-text", "error-soft"],
  ])("%s on %s meets WCAG AA for small text", (fg, bg) => {
    expect(contrast(fg, bg)).toBeGreaterThanOrEqual(4.5);
  });

  it.each(["bg", "surface", "surface-secondary"])("control outline on %s meets 3:1", (bg) => {
    expect(contrast("control", bg)).toBeGreaterThanOrEqual(3);
  });

  it("keeps primary actions separate from brand accents and semantic colors", () => {
    expect(config).toContain('primary: { DEFAULT: token("action")');
    expect(config).toContain('accent: { seal: token("accent-control")');
    expect(config).toContain('bright: token("brand")');
    expect(config).not.toMatch(/#[\da-f]{3,8}\b/i);
    expect(rgb("action")).not.toEqual(rgb("brand"));
    expect(rgb("success-text")[1]).toBeGreaterThan(rgb("success-text")[0]);
    expect(rgb("error-text")[0]).toBeGreaterThan(rgb("error-text")[1]);
  });

  it("uses tokens for landing colors; preserves the existing media mask", () => {
    postcss.parse(loginCss).walkDecls((decl) => {
      if (!decl.prop.includes("mask-image")) expect(decl.value).not.toMatch(/#[\da-f]{3,8}\b/i);
    });
    expect(loginCss).toContain("mask-composite: intersect");
    expect(loginCss).toContain("background: rgb(var(--nova-action))");
    expect(loginCss).toContain("background: rgb(var(--nova-action-hover))");
    expect(loginCss).toContain("background: rgb(var(--nova-action-active))");
  });

  it("compiles variable colors without unresolved alpha placeholders", async () => {
    const result = await postcss([tailwindcss({ config: "./tailwind.config.js" })]).process(css, { from: "src/index.css" });
    expect(result.css).not.toContain("<alpha-value>");
    expect(result.css).toContain("outline: 2px solid rgb(var(--nova-accent-control))");
  });

  it("covers autofill without dropping text color or keyboard focus", () => {
    expect(css).toContain("input:is(:-webkit-autofill, :autofill)");
    expect(css).toContain("1000px rgb(var(--nova-bg)) inset !important");
    expect(css).toContain("-webkit-text-fill-color: rgb(var(--nova-text)) !important");
    expect(css).toContain(":focus-visible { outline: 2px solid");
  });
});
