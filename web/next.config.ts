import type { NextConfig } from "next";

// The screens call /api/* on their own origin, so the browser never needs a second origin or CORS. A build is a
// static export (`out/`) that the Python API serves itself (src/rfp_assistant/api.py): one process, one port, no
// Node at run time. `next dev` instead forwards /api/* to a separately running API; static export forbids
// rewrites, so each mode gets only its own setting.
const API_URL = process.env.RFP_API_URL ?? "http://127.0.0.1:8511";

const nextConfig: NextConfig = process.env.NODE_ENV === "production"
  ? { output: "export", trailingSlash: true }
  : { async rewrites() { return [{ source: "/api/:path*", destination: `${API_URL}/api/:path*` }]; } };

export default nextConfig;
