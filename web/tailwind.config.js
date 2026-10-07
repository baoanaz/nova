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
        sm: "0 4px 20px rgb(39 22 16 / 0.06)",
        xl: "0 24px 64px rgb(39 22 16 / 0.12)",
      },
      // 文字橙比按钮底色稍深，确保浅橙选中背景上的小字也达到 4.5:1。
      textColor: { accent: { seal: "#A1462D" } },
      colors: {
        paper: { base: "#FFF7F2", raised: "#FFFCF9", card: "#FFFFFD" },
        ink: { primary: "#271610", muted: "#816055", line: "#D7C5BC", control: "#967B6B" },
        accent: { seal: "#B95336", soft: "#FBE7DA", bright: "#D97757", contrast: "#FFFFFF" },
        brand: { gold: "#8A5A24" },
        success: { soft: "#ECFDF5", text: "#047857", line: "#45866D" },
        warning: { soft: "#FFF7ED", text: "#92400E", line: "#B8773B" },
        error: { soft: "#FFF1F2", text: "#BE123C", line: "#C86E7C" },
        danger: { base: "#BD3434", hover: "#A12727" },
      },
    },
  },
  plugins: [],
};
