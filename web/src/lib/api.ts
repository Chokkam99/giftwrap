import type {
  ChatResponse,
  GiftInfo,
  GiftPickResponse,
  GiftSearchResponse,
  MoreResponse,
  ProductDetail,
  ProductSelection,
} from "./types";

/** Thrown for any non-2xx from the Python API. `detail` carries FastAPI's
 *  HTTPException body when there is one, so callers can show the server's own
 *  refusal text (budget guard, merchant scope, expired gift link) instead of a
 *  generic message. Surfacing the real refusal is the point of the product. */
export class ApiError extends Error {
  readonly status: number;
  readonly detail?: string;

  constructor(status: number, detail?: string) {
    super(detail || `Request failed with ${status}`);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    ...init,
    headers:
      init?.body !== undefined
        ? { "Content-Type": "application/json", ...init?.headers }
        : init?.headers,
  });

  if (!res.ok) {
    let detail: string | undefined;
    try {
      const body = (await res.json()) as { detail?: unknown };
      if (typeof body?.detail === "string") detail = body.detail;
    } catch {
      // Non-JSON error body; fall back to the status alone.
    }
    throw new ApiError(res.status, detail);
  }

  return (await res.json()) as T;
}

export function sendChat(
  conversationId: string,
  message: string,
  selection?: ProductSelection | null,
): Promise<ChatResponse> {
  return request<ChatResponse>("/api/chat", {
    method: "POST",
    body: JSON.stringify({
      conversation_id: conversationId,
      message,
      selection: selection ?? null,
    }),
  });
}

export function loadMoreChat(conversationId: string): Promise<MoreResponse> {
  return request<MoreResponse>(
    `/api/chat/${encodeURIComponent(conversationId)}/more`,
    { method: "POST" },
  );
}

export function fetchProductDetail(
  store: string,
  id: string,
  budget?: number | null,
): Promise<ProductDetail> {
  const params = new URLSearchParams({ store, id });
  if (budget != null) params.set("budget", String(budget));
  return request<ProductDetail>(`/api/product?${params.toString()}`);
}

export function fetchGiftInfo(token: string): Promise<GiftInfo> {
  return request<GiftInfo>(`/api/gift/${encodeURIComponent(token)}/info`);
}

export function searchGift(
  token: string,
  query: string,
  store?: string | null,
): Promise<GiftSearchResponse> {
  return request<GiftSearchResponse>(
    `/api/gift/${encodeURIComponent(token)}/search`,
    {
      method: "POST",
      body: JSON.stringify(store ? { query, store } : { query }),
    },
  );
}

export function loadMoreGift(token: string): Promise<MoreResponse> {
  return request<MoreResponse>(
    `/api/gift/${encodeURIComponent(token)}/more`,
    { method: "POST" },
  );
}

export function pickGift(
  token: string,
  product: { product_id: string; title: string; price: string },
): Promise<GiftPickResponse> {
  return request<GiftPickResponse>(
    `/api/gift/${encodeURIComponent(token)}/pick`,
    { method: "POST", body: JSON.stringify(product) },
  );
}
