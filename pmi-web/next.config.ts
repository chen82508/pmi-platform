import type { NextConfig } from "next";

// NEXT_PUBLIC_PMI_STATIC=1 builds a read-only static export of the dashboard
// (GitHub Pages) from a running demo stack; see `just web-static`. The default
// build is the live server the Docker image runs.
const STATIC_EXPORT = process.env.NEXT_PUBLIC_PMI_STATIC === "1";

// Which files become routes. `page.live.tsx` / `page.export.tsx` are route
// entries for one build only (see app/pmi_dashboard/indexes/[id]/index-page.tsx);
// in the other build their name isn't `page`, so they are plain modules. Route
// handlers are .ts and proxy live pmi-api calls, which an export cannot hold,
// while every page and layout is .tsx.
const LIVE_EXTENSIONS = ["live.tsx", "tsx", "ts"];
const EXPORT_EXTENSIONS = ["export.tsx", "tsx"];

const config: NextConfig = {
  reactStrictMode: true,
  ...(STATIC_EXPORT
    ? {
        output: "export",
        pageExtensions: EXPORT_EXTENSIONS,
        // A GitHub project site is served under /<repo>/.
        basePath: process.env.PMI_WEB_BASE_PATH || "",
        // Pages serves dir/index.html for /dir/, not /dir.html for /dir.
        trailingSlash: true,
        env: { NEXT_PUBLIC_PMI_SNAPSHOT_AT: new Date().toISOString() },
      }
    : {
        // Self-contained server bundle for the slim production image.
        output: "standalone",
        pageExtensions: LIVE_EXTENSIONS,
      }),
};

export default config;
