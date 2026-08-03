import type { NextConfig } from "next";

// The FastAPI app (main.py) stays the single source of truth for the agent
// loop, UCP, Prava and checkout. `app/api/[...path]/route.ts` proxies browser
// requests at runtime, which lets Vercel Services inject its private backend
// binding without exposing the API publicly or baking a deployment URL into a
// build-time rewrite.

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
};

export default nextConfig;
