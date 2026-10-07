/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      fontFamily: {
        sans: ["Inter", "ui-sans-serif", "system-ui", "-apple-system", '"Segoe UI"', '"PingFang SC"', '"Microsoft YaHei"', "sans-serif"],
        mono: ["ui-monospace", "SFMono-Regular", "Menlo", "Consolas", "monospace"],
      },
      borderRadius: { DEFAULT: "0.5rem", sm: "0.25rem", md: "0.625rem", lg: "0.75rem", xl: "1rem", "2xl": "1.25rem" },
      boxShadow: {
        sm: "0 4px 20px rgb(0 0 0 / 0.12)",
        xl: "0 24px 64px rgb(0 0 0 / 0.3)",
      },
      colors: {
        paper: { base: "#0B0F14", raised: "#10161F", card: "#171F2B" },
        ink: { primary: "#E9EEF5", muted: "#A0AEC0", line: "#2A3545", control: "#718198" },
        accent: { seal: "#93C5FD", soft: "#1B3049", bright: "#BFDBFE", contrast: "#0B0F14" },
        brand: { gold: "#D6C3A5" },
        success: { soft: "#122B26", text: "#6EE7B7", line: "#39896C" },
        warning: { soft: "#302519", text: "#FDBA74", line: "#B78248" },
        error: { soft: "#321C25", text: "#FDA4AF", line: "#B96777" },
        danger: { base: "#BE123C", hover: "#9F1239" },
      },
    },
  },
  plugins: [],
};
