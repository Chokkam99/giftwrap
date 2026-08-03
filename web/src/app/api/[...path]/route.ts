import { NextRequest } from "next/server";

type RouteContext = { params: Promise<{ path: string[] }> };

/**
 * Same-origin API façade for the browser.
 *
 * Locally, GIFTWRAP_API_URL points at Uvicorn. On Vercel Services it is a
 * runtime-only private binding to the FastAPI service. Keeping this as a route
 * handler, rather than a build-time Next rewrite, is essential because Vercel
 * bindings do not exist while Next is being built.
 */
async function proxy(request: NextRequest, { params }: RouteContext) {
  const apiBase = process.env.GIFTWRAP_API_URL;
  if (!apiBase) {
    return Response.json(
      { detail: "GiftWrap API is not configured." },
      { status: 503 },
    );
  }

  const { path } = await params;
  const target = new URL(path.map(encodeURIComponent).join("/"), `${apiBase.replace(/\/$/, "")}/`);
  target.search = request.nextUrl.search;

  const headers = new Headers();
  for (const name of ["accept", "content-type"]) {
    const value = request.headers.get(name);
    if (value) headers.set(name, value);
  }

  const body = ["GET", "HEAD"].includes(request.method)
    ? undefined
    : await request.arrayBuffer();

  try {
    const upstream = await fetch(target, {
      method: request.method,
      headers,
      body,
      cache: "no-store",
      redirect: "manual",
    });
    const responseHeaders = new Headers();
    const contentType = upstream.headers.get("content-type");
    if (contentType) responseHeaders.set("content-type", contentType);
    return new Response(upstream.body, {
      status: upstream.status,
      headers: responseHeaders,
    });
  } catch {
    return Response.json(
      { detail: "GiftWrap API is temporarily unavailable." },
      { status: 502 },
    );
  }
}

export const GET = proxy;
export const POST = proxy;
