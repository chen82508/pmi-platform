import "./globals.css";

import type { Metadata } from "next";
import { Inter } from "next/font/google";

// Self-hosted at build time by next/font: no runtime request to Google.
const inter = Inter({ subsets: ["latin"], variable: "--font-inter", display: "swap" });

export const metadata: Metadata = {
  title: {
    default: "PMI Platform",
    template: "%s — PMI Platform",
  },
  description:
    "Declarative prediction-market indices: track, explain and backtest PMIs.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className={inter.variable}>
      <body className="min-h-screen flex flex-col font-sans">{children}</body>
    </html>
  );
}
