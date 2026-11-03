/**
 * Static-export build mode (`NEXT_PUBLIC_PMI_STATIC=1`, see next.config.ts):
 * the dashboard is rendered once at build time against a demo stack and
 * published as plain files, so there is no pmi-api behind it at runtime.
 * NEXT_PUBLIC_ so client components (backtest panel, header badge) can tell
 * too; both values are inlined at build.
 */
export const STATIC_EXPORT = process.env.NEXT_PUBLIC_PMI_STATIC === "1";

/** ISO instant the static export was built; empty in the live build. */
export const SNAPSHOT_AT = process.env.NEXT_PUBLIC_PMI_SNAPSHOT_AT ?? "";
