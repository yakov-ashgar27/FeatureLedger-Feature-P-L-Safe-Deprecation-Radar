/** @type {import('next').NextConfig} */
const API_URL = process.env.API_URL || "http://localhost:8000";

const isDev = process.env.NODE_ENV !== "production";
const secure = (process.env.APP_URL || "").startsWith("https://");

// Everything the dashboard loads comes from its own origin. Next.js inlines its bootstrap scripts and
// styles, hence 'unsafe-inline'; no third-party script, frame or connection is allowed, and no site may
// frame the dashboard (clickjacking).
const csp = [
  "default-src 'self'",
  `script-src 'self' 'unsafe-inline'${isDev ? " 'unsafe-eval'" : ""}`,
  "style-src 'self' 'unsafe-inline'",
  "img-src 'self' data: blob:",
  "font-src 'self' data:",
  `connect-src 'self'${isDev ? " ws: wss:" : ""}`,
  "frame-ancestors 'none'",
  "base-uri 'self'",
  "form-action 'self'",
  "object-src 'none'",
].join("; ");

const securityHeaders = [
  { key: "Content-Security-Policy", value: csp },
  { key: "X-Content-Type-Options", value: "nosniff" },
  { key: "X-Frame-Options", value: "DENY" },
  { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
  { key: "Permissions-Policy", value: "camera=(), microphone=(), geolocation=(), payment=()" },
  { key: "Cross-Origin-Opener-Policy", value: "same-origin" },
  ...(secure ? [{ key: "Strict-Transport-Security", value: "max-age=31536000; includeSubDomains" }] : []),
];

const nextConfig = {
  poweredByHeader: false,
  async headers() {
    return [{ source: "/:path*", headers: securityHeaders }];
  },
  reactStrictMode: true,
  eslint: { ignoreDuringBuilds: true },
  // Browser calls go to /api/v1/* on the dashboard's own origin and are proxied to FastAPI,
  // so no CORS and no API URL baked into the client bundle. Evaluated at `next start`.
  async rewrites() {
    return [{ source: "/api/v1/:path*", destination: `${API_URL}/api/v1/:path*` }];
  },
};

export default nextConfig;
