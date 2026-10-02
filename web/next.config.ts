import type { NextConfig } from "next";

// The screens call /api/* on their own origin; Next forwards it to the Python API (src/rfp_assistant/api.py),
// so the browser never needs a second origin or CORS.
const API_URL = process.env.RFP_API_URL ?? "http://127.0.0.1:8511";

const nextConfig: NextConfig = {
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${API_URL}/api/:path*` }];
  },
};

export default nextConfig;
