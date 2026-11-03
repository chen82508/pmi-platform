import Link from "next/link";

import { NavLinks } from "@/components/NavLinks";
import { SNAPSHOT_AT, STATIC_EXPORT } from "@/lib/static-export";
import { formatAsOf } from "@/lib/text";

/**
 * Operational dashboard chrome: index list, source health and per-index
 * detail (score, history, drivers, on-demand refresh, backtest). Pages own
 * their full-width hero band, so the main area is not width-constrained here.
 */
export default function DashboardLayout({ children }: { children: React.ReactNode }) {
  return (
    <>
      <header className="bg-navy text-white border-b border-white/10">
        <div className="container-x flex h-14 items-center justify-between">
          <Link href="/pmi_dashboard" className="flex items-center gap-2.5">
            <svg width="26" height="26" viewBox="0 0 48 48" aria-hidden="true">
              <rect width="48" height="48" rx="11" fill="#2563EB" />
              <path
                d="M11 32 L19 22 L26 27 L37 14"
                fill="none"
                stroke="#fff"
                strokeWidth="4"
                strokeLinecap="round"
                strokeLinejoin="round"
              />
            </svg>
            <span className="text-[15px] font-semibold tracking-tight">PMI Platform</span>
          </Link>
          <div className="flex items-center gap-4">
            <NavLinks />
            <span className="hidden items-center gap-1.5 rounded-full border border-white/15 px-2.5 py-1 text-xs text-white/70 sm:flex">
              <span className="h-1.5 w-1.5 rounded-full bg-positive" />
              Fixture data
              {STATIC_EXPORT && ` · snapshot ${formatAsOf(SNAPSHOT_AT)}`}
            </span>
          </div>
        </div>
      </header>
      <main className="flex-1 pb-16">{children}</main>
      <footer className="border-t border-surface-border bg-surface text-xs text-ink-muted">
        <div className="container-x flex justify-between py-4">
          <span>Prediction-market indices — scores, lineage and backtests</span>
          <span className="font-mono">pmi-web</span>
        </div>
      </footer>
    </>
  );
}
