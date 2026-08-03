// Wire types mirroring main.py. Kept deliberately close to the Python shapes
// so a field rename on the server surfaces here as a type error rather than
// as silently-undefined UI.

/** A catalog product as returned in `PRODUCT_FIELDS`, plus the sandbox extras. */
export interface Product {
  id: string;
  title: string | null;
  price: string | null;
  currency: string | null;
  image_url: string | null;
  product_url: string | null;
  merchant: string | null;
  variant_id: string | null;
  /** "sandbox_checkout" marks the controlled demo-store card, not a UCP result. */
  catalog_source?: string;
  checkout_note?: string;
  /** Present only on the sandbox card: the verified full checkout total. */
  payment_cap?: string;
  stub?: boolean;
}

export interface VariantChip {
  id: string | null;
  label: string;
  price: string | null;
  currency: string | null;
  available: boolean;
}

export interface BudgetHeadroom {
  budget: number;
  spend: number;
  remaining: number;
  currency: string;
}

/** GET /product */
export interface ProductDetail extends Product {
  images: string[];
  description: string;
  variants: VariantChip[];
  budget_headroom?: BudgetHeadroom;
}

export type ChatAction =
  | { type: "approve_payment"; iframe_url: string; session_id: string }
  | {
      type: "receipt";
      order_id: string | null;
      amount: string | null;
      merchant: string | null;
    }
  | { type: "gift_link"; url: string; token: string };

/** POST /chat */
export interface ChatResponse {
  reply: string;
  cards: Product[] | null;
  action: ChatAction | null;
  budget: string | null;
  has_more: boolean;
  /** Only present on branches that ship the controlled demo-store card.
   *  `codex/allow-cross-store-checkout` removed it, so treat it as optional. */
  sandbox_checkout_item?: Product | null;
}

/** The structured approval the buyer UI sends alongside its natural-language
 *  confirmation. Mirrors `ProductSelection` in main.py. */
export interface ProductSelection {
  id: string;
  title?: string | null;
  price?: string | null;
  store?: string | null;
  product_url?: string | null;
}

/** POST /chat/{id}/more and POST /gift/{token}/more */
export interface MoreResponse {
  products: Product[];
  has_more: boolean;
  message?: string;
}

/** GET /gift/{token}/info */
export interface GiftInfo {
  budget: number;
  currency: string;
  note: string;
  status: "awaiting_pick" | "picked";
  picked_product: PickedProduct | null;
  stores: string[];
}

export interface PickedProduct {
  product_id: string;
  title: string;
  price: string;
}

/** POST /gift/{token}/search */
export interface GiftSearchResponse {
  stores_searched: string[];
  count: number;
  products: Product[];
  max_price_enforced: number;
  filtered_out_over_budget?: number;
  has_more?: boolean;
  stub?: boolean;
}

/** POST /gift/{token}/pick */
export interface GiftPickResponse {
  ok: boolean;
  status?: string;
  picked_product?: PickedProduct;
  error?: string;
}
