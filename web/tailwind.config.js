/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      fontFamily: {
        sans: [
          "Inter", "ui-sans-serif", "system-ui", "-apple-system", '"Segoe UI"',
          '"PingFang SC"', '"Microsoft YaHei"', "sans-serif",
        ],
        mono: ["ui-monospace", "SFMono-Regular", "Menlo", "Consolas", "monospace"],
      },
      // 方角与偏移阴影是全站统一的视觉语法；圆形身份徽章与开关仍保留 rounded-full。
      borderRadius: { DEFAULT: "0", sm: "0", md: "0", lg: "0", xl: "0", "2xl": "0" },
      boxShadow: {
        sm: "4px 4px 0 rgb(39 22 16 / 0.14)",
        xl: "8px 8px 0 rgb(39 22 16 / 0.18)",
      },
      // 沿用已有语义 token，让所有业务页面同步切换主题。
      colors: {
        paper: {
          base: "#fff7f2",
          raised: "#fffcf9",
          card: "#fffffd",
        },
        ink: {
          primary: "#271610",
          muted: "#816055",
          line: "#d7c5bc",
        },
        accent: {
          seal: "#b95336", // 加深珊瑚橙，保证白色按钮文字的对比度。
          soft: "#fbe7da",
          bright: "#d97757",
        },
        danger: { base: "#bd3434", hover: "#a12727" },
      },
    },
  },
  plugins: [],
};
