# Shopify development store setup (for the paid-checkout demo)

**Who this is for:** Rithvik. ~20 minutes, no cost, no credit card, no real money.

**Why we need it:** the agent pays with a Prava one-time virtual card. Real
merchants (GIVA etc.) run live gateways that will never accept a sandbox test
card, and we must not attempt real purchases there. So: **real merchants for
catalog discovery (UCP, read-only), our own Shopify development store with a
test gateway for the actual paid checkout.** The order id we show at the end is
a genuine Shopify order in an admin we control.

The automation in `checkout/playwright_checkout.py` hard-refuses any checkout
whose host is not `*.myshopify.com` (and permanently refuses a denylist of real
merchant domains), so this store is the only place it can spend.

---

## 1. Create a Partner account and a development store

1. Sign up at <https://partners.shopify.com> (free; personal email is fine).
2. In the Partner dashboard: **Stores → Add store → Create development store**.
3. Choose **"Store for testing and development"** (NOT a "client store" —
   testing stores are what get free test payments).
4. Name it something obvious, e.g. `agentic-gifting-demo`. Store URL becomes
   `agentic-gifting-demo.myshopify.com`.
5. Store purpose / build version: defaults are fine. Data: "Start with test
   data" is optional — we add our own products below.
6. Set the store currency to **INR** (Settings → General → Store defaults →
   Store currency) so prices line up with the Prava card limits in the demo.

## 2. Add 1–2 gift products

Admin → **Products → Add product**:

| Field | Value |
| --- | --- |
| Title | `Silver Pendant` |
| Price | `2499` (INR) |
| Inventory | untick "Track quantity" (avoids out-of-stock failures) |
| Shipping | untick "This is a physical product" **or** keep it physical and add a shipping rate (see §5) |
| Status | **Active**, sales channel **Online Store** enabled |

Add a second, pricier item to demo the budget cap, e.g. `Gold Hoop Earrings`
at `9999`.

Copy the storefront URL of the first product — it looks like
`https://agentic-gifting-demo.myshopify.com/products/silver-pendant`. That goes
into `.env` as `SHOPIFY_DEV_STORE_PRODUCT_URL`.

## 3. Enable a test payment gateway

Development stores cannot take real payments, so one of these two must be on.
**Try Shopify Payments test mode first.**

### Option A (preferred) — Shopify Payments test mode

Admin → **Settings → Payments → Shopify Payments**. On a development store it
activates in test mode (banner: "Your store is in test mode"); if it asks to
"Complete account setup", look for **Manage → Test mode → Enable test mode**.

Why we prefer it: it accepts **arbitrary well-formed test PANs**, so the real
16-digit Prava token, its dynamic CVV, and its expiry all travel through the
normal card fields. That is the flow we actually want to demo.

Useful behaviour: Shopify's test-mode cards approve on `4242 4242 4242 4242`
and decline on `4000 0000 0000 0002`. Our token is neither, so treat whatever
it returns as the real result — an approval is a genuine end-to-end success,
and a decline is still a real, honest gateway response we surface in the UI.

> If Shopify Payments is unavailable in your region/account for dev stores,
> fall through to Option B.

### Option B (fallback) — Bogus Gateway

Admin → **Settings → Payments → (choose additional payment methods) →
"(for testing) Bogus Gateway"** → Activate.

Bogus Gateway **ignores real card numbers**. It keys entirely off the digit you
type as the card number:

| Card number | Result |
| --- | --- |
| `1` | approved |
| `2` | failed / declined |
| `3` | gateway error |

Expiry: any future date. CVV: any 3 digits (`111` works). Name: anything.

Because it will *not* exercise the Prava token, the code supports it as an
explicit, clearly-labelled **gateway-simulation mode**:

```bash
BOGUS_GATEWAY=1 python scripts/checkout_smoke.py --product-url ... --token <real token> --cvv <cvv> --month 12 --year 2027
```

With `BOGUS_GATEWAY` set, `effective_card_number()` substitutes `1` (or `2`/`3`
if you set those, to force a decline for the red-team demo) in place of the
token. Everything else — mint, budget checks, order capture, status reporting —
still runs for real. Say so out loud in the demo if we end up here: "the card
field is gateway-simulated; the mint, the caps and the order are real."

**Recommendation:** Shopify Payments test mode. Bogus only if A is blocked.

## 4. Storefront password (the dev-store gate)

New dev stores hide the storefront behind a password page.

- **Preferred:** Admin → **Online Store → Preferences → Password protection**
  → untick **Restrict access to visitors with the password**. Simplest, and the
  automation then has one less failure mode.
- **If you must keep it on:** copy the password from that same screen into
  `.env` as `SHOPIFY_STOREFRONT_PASSWORD=...`. The automation handles the Dawn
  theme's collapsed gate (it clicks "Enter using password" before typing).
  Without the env var it raises `CheckoutError` rather than guessing.

## 5. Keep checkout frictionless

- **Settings → Checkout → Customer accounts → "Don't use accounts"** (guest
  checkout). If accounts are required the automation cannot complete.
- **Settings → Checkout → Contact method → Email.**
- **Settings → Shipping and delivery:** make sure the shipping zone covers
  **India** with at least one rate (a ₹0 "Standard" rate is ideal — it keeps
  the total predictable against the card limit). If no rate matches the
  address, checkout stalls before payment.
- Marketing/consent checkboxes: leave defaults; the automation doesn't tick
  anything.
- The automation ships to a Bengaluru address (`12 MG Road`, Karnataka,
  `560001`, `+91…`). Override per-field with `CHECKOUT_SHIP_CITY`,
  `CHECKOUT_SHIP_ZIP`, etc. if you'd rather use a different one.

## 6. Fill in `.env`

```dotenv
SHOPIFY_DEV_STORE_URL=https://agentic-gifting-demo.myshopify.com
SHOPIFY_DEV_STORE_PRODUCT_URL=https://agentic-gifting-demo.myshopify.com/products/silver-pendant
SHOPIFY_STOREFRONT_PASSWORD=          # only if you kept the gate on
CHECKOUT_HEADLESS=true                # false to watch the browser drive
# BOGUS_GATEWAY=1                     # only in Bogus fallback mode
```

## 7. Verify (do this the moment the store exists)

Run everything from the repo root; the project venv is uv-managed.

```bash
uv sync                          # once, if you haven't already
uv run playwright install chromium   # once per machine

# read-only: loads the product page, checks add-to-cart. Buys nothing.
uv run python scripts/checkout_smoke.py --verify-store \
  --product-url https://agentic-gifting-demo.myshopify.com/products/silver-pendant

# fills the whole checkout but never clicks Pay
uv run python scripts/checkout_smoke.py --dry-run --headed \
  --product-url ... --token 4111111111111111 --cvv 123 --month 12 --year 2027

# the real thing, with a freshly minted Prava card
uv run python scripts/checkout_smoke.py --headed \
  --product-url ... --token <token> --cvv <dynamic cvv> --month 12 --year 2027
```

Exit codes: `0` approved · `1` declined · `2` refused/bad arguments ·
`3` failed · `4` setup error (`CheckoutError`).

Confirm in Admin → **Orders** that the order appears (test orders are tagged as
such). That order id is what the agent reports back to the user.

## 8. Troubleshooting

| Symptom | Fix |
| --- | --- |
| `CheckoutError: Storefront is password-protected` | §4 — disable the gate or set `SHOPIFY_STOREFRONT_PASSWORD`. |
| `No add-to-cart button found` | Product isn't Active / not on the Online Store channel, or the URL isn't a product page. |
| `Could not reach the checkout` | Cart empty (add-to-cart silently failed) or accounts are required — §5. |
| `Card number field not found` | No test gateway is active — §3. |
| Checkout stalls at shipping | No shipping rate covers India — §5. |
| `Host … is not a *.myshopify.com development store` | You pointed it at the wrong store. Custom dev-store domains need `CHECKOUT_ALLOW_ANY_HOST=1`; real merchants are refused outright. |
| Want to watch it run | `--headed`, or `CHECKOUT_HEADLESS=false`. Failure screenshots land in `$CHECKOUT_ARTIFACT_DIR` (default: temp dir). |
