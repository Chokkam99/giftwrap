import type { NextConfig } from "next";

// The FastAPI app (main.py) stays the single source of truth for the agent
// loop, UCP, Prava and checkout. This front end never reimplements any of it:
// it proxies same-origin /api/* paths through to the Python server, so the
// browser has no CORS story and no API base URL to configure.
const API = process.env.GIFTWRAP_API_URL ?? "http://127.0.0.1:8077";

const nextConfig: NextConfig = {
  images: {
    // Live merchant catalog media. `images.domains` is removed in Next 16;
    // remotePatterns is the only supported form.
    remotePatterns: [
      { protocol: "https", hostname: "cdn.shopify.com" },
      { protocol: "https", hostname: "**.myshopify.com" },
      { protocol: "https", hostname: "**.shopifycdn.com" },
    ],
  },
  async rewrites() {
    // Namespaced under /api/ so nothing here can collide with the
    // /gift/[token] page route.
    return [
      { source: "/api/chat", destination: `${API}/chat` },
      { source: "/api/chat/:id/more", destination: `${API}/chat/:id/more` },
      { source: "/api/product", destination: `${API}/product` },
      { source: "/api/health", destination: `${API}/health` },
      { source: "/api/gift/:token/info", destination: `${API}/gift/:token/info` },
      { source: "/api/gift/:token/search", destination: `${API}/gift/:token/search` },
      { source: "/api/gift/:token/more", destination: `${API}/gift/:token/more` },
      { source: "/api/gift/:token/pick", destination: `${API}/gift/:token/pick` },
    ];
  },
};

export default nextConfig;
