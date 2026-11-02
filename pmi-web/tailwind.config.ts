import type { Config } from "tailwindcss";

// Palette: navy #0A2540 / #1D3A5F, canvas #F6F9FC, interactive blue #2563EB,
// positive #10B981, negative #EF4444. Everything else is derived from these.
const config: Config = {
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}", "./lib/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        navy: { DEFAULT: "#0A2540", 2: "#1D3A5F" },
        canvas: "#F6F9FC",
        brand: { DEFAULT: "#2563EB", soft: "#EAF1FE" },
        positive: { DEFAULT: "#10B981", soft: "#E7F8F1" },
        negative: { DEFAULT: "#EF4444", soft: "#FDECEC" },
        ink: { DEFAULT: "#0A2540", muted: "#5B6B82", faint: "#8A97AB" },
        surface: { DEFAULT: "#FFFFFF", muted: "#F6F9FC", border: "#E3E8EF" },
      },
      fontFamily: {
        sans: ["var(--font-inter)", "ui-sans-serif", "system-ui", "-apple-system", "Segoe UI", "sans-serif"],
        mono: ["ui-monospace", "SFMono-Regular", "Menlo", "monospace"],
      },
      boxShadow: {
        card: "0 1px 2px rgba(10, 37, 64, 0.04), 0 4px 16px rgba(10, 37, 64, 0.06)",
        lift: "0 2px 4px rgba(10, 37, 64, 0.06), 0 12px 32px rgba(10, 37, 64, 0.12)",
      },
    },
  },
  plugins: [],
};

export default config;
