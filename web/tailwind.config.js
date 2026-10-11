const token = (name) => `rgb(var(--nova-${name}) / <alpha-value>)`;

/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      fontFamily: {
        sans: ["Inter", "ui-sans-serif", "system-ui", "-apple-system", '"Segoe UI"', '"PingFang SC"', '"Microsoft YaHei"', "sans-serif"],
        mono: ["ui-monospace", "SFMono-Regular", "Menlo", "Consolas", "monospace"],
      },
      borderRadius: { DEFAULT: "0", sm: "0", md: "0.625rem", lg: "0.75rem", xl: "1rem", "2xl": "1.25rem" },
      boxShadow: {
        sm: "var(--nova-shadow-sm)",
        xl: "var(--nova-shadow-xl)",
      },
      // 装饰橙、白字按钮底色与小字号强调文字分别管理。
      textColor: { accent: { seal: token("accent-text") } },
      colors: {
        paper: { base: token("bg"), raised: token("surface-secondary"), card: token("surface"), warm: token("surface-warm"), hero: token("hero") },
        ink: { primary: token("text"), muted: token("text-secondary"), subtle: token("text-subtle"), line: token("border"), control: token("control") },
        primary: { DEFAULT: token("action"), hover: token("action-hover"), active: token("action-active"), contrast: token("on-action") },
        accent: { seal: token("accent-control"), soft: token("selected"), bright: token("brand"), contrast: token("on-action") },
        brand: { gold: token("brand-gold") },
        success: { soft: token("success-soft"), text: token("success-text"), line: token("success") },
        warning: { soft: token("warning-soft"), text: token("warning-text"), line: token("warning-line") },
        error: { soft: token("error-soft"), text: token("error-text"), line: token("error-line") },
        danger: { base: token("danger"), hover: token("danger-hover") },
      },
    },
  },
  plugins: [],
};
