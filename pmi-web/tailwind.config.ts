import type { Config } from "tailwindcss";

const config: Config = {
  content: [
    "./app/**/*.{ts,tsx}",
    "./components/**/*.{ts,tsx}",
    "./lib/**/*.{ts,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        // Borrowed from micah-frontend/war-index palette (Figma export) so
        // pmi-web and the legacy dashboards visually rhyme. Trim or replace
        // when a real design system lands.
        ink: {
          DEFAULT: "#30374F",
          muted: "#6B7280",
        },
        accent: {
          DEFAULT: "#6594AB",
          warm: "#F7B27A",
        },
        surface: {
          DEFAULT: "#FFFFFF",
          muted: "#F4F5F8",
          border: "#B9C0D4",
        },
      },
      fontFamily: {
        sans: ["ui-sans-serif", "system-ui", "-apple-system", "Segoe UI", "Roboto", "sans-serif"],
        serif: ["Spectral", "Georgia", "Cambria", "serif"],
      },
    },
  },
  plugins: [],
};

export default config;
