/** Curated merchant labels. The API returns raw storefront domains; showing
 *  "giva-jewelry.myshopify.com" on a product card leaks plumbing at the buyer. */
const STORE_LABELS: Record<string, string> = {
  "giva-jewelry.myshopify.com": "GIVA",
  "mamaearth.in": "Mamaearth",
  "salty.co.in": "Salty",
  "plumgoodness.com": "Plum",
  "xyxxcrew.com": "XYXX",
  "agentic-gifting-demo.myshopify.com": "GiftWrap Demo Store",
};

export function storeLabel(domain?: string | null): string {
  if (!domain) return "";
  return STORE_LABELS[domain] ?? domain;
}

/** Prices arrive as decimal strings ("2799.00"). Render them without ever
 *  doing float maths on money: the string is the source of truth, and only
 *  the thousands grouping is computed. */
export function formatPrice(
  price?: string | number | null,
  currency?: string | null,
): string {
  if (price === undefined || price === null || price === "") return "";

  const raw = typeof price === "number" ? price.toFixed(2) : String(price).trim();
  const numeric = Number(raw.replace(/,/g, ""));
  if (!Number.isFinite(numeric)) return String(price);

  // Drop a trailing ".00" — every catalog price has it and it adds nothing.
  const hasPaise = Math.round(numeric * 100) % 100 !== 0;
  const grouped = numeric.toLocaleString("en-IN", {
    minimumFractionDigits: hasPaise ? 2 : 0,
    maximumFractionDigits: 2,
  });

  if (currency === "INR" || !currency) return `₹${grouped}`;
  return `${currency} ${grouped}`;
}

/** Parses "₹3,000" / "2999.00" / 2999 into a number, matching main.py's
 *  `_parse_amount` so the client and server agree on what a budget is. */
export function parseAmount(value?: string | number | null): number | null {
  if (typeof value === "number") return Number.isFinite(value) ? value : null;
  if (typeof value !== "string") return null;
  const match = value.match(/\d[\d,]*(?:\.\d+)?/);
  if (!match) return null;
  const parsed = Number(match[0].replace(/,/g, ""));
  return Number.isFinite(parsed) ? parsed : null;
}

export function makeConversationId(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (c) => {
    const r = (Math.random() * 16) | 0;
    const v = c === "x" ? r : (r & 0x3) | 0x8;
    return v.toString(16);
  });
}
