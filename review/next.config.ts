import type { NextConfig } from "next";

// A static export (`out/`) that review/server.py serves with the run folders on one loopback port; no Node at run time.
const nextConfig: NextConfig = { output: "export", trailingSlash: true };

export default nextConfig;
