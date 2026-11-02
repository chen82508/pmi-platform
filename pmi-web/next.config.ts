import type { NextConfig } from "next";

const config: NextConfig = {
  reactStrictMode: true,
  // Self-contained server bundle for the slim production image.
  output: "standalone",
};

export default config;
