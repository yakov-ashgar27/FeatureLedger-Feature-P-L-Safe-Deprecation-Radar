/** @type {import('next').NextConfig} */
const API_URL = process.env.API_URL || "http://localhost:8000";

const nextConfig = {
  reactStrictMode: true,
  eslint: { ignoreDuringBuilds: true },
  // Browser calls go to /api/v1/* on the dashboard's own origin and are proxied to FastAPI,
  // so no CORS and no API URL baked into the client bundle. Evaluated at `next start`.
  async rewrites() {
    return [{ source: "/api/v1/:path*", destination: `${API_URL}/api/v1/:path*` }];
  },
};

export default nextConfig;
