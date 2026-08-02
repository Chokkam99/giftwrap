# GiftWrap — agentic gifting powered by Prava

GiftWrap is an agentic gifting assistant. A buyer tells it who they're
shopping for, a budget, a store, and an optional gift note; the agent takes
it from there. It searches a real Shopify store's catalog over the Universal
Commerce Protocol (UCP), proposes gift options within budget, and — once the
buyer approves a pick — asks Prava to mint a one-time, merchant-scoped,
amount-capped virtual card so spend physically cannot exceed the budget the
buyer set. A Playwright-driven browser session completes checkout on the
merchant's real storefront using that card, and the buyer gets back an order
receipt.

The point isn't just "an LLM that can shop" — it's that the safety boundary
lives below the model. Even if the agent hallucinates, gets prompt-injected
by a malicious product page, or simply misbehaves, the card it was handed
cannot spend more than the approved amount or be used anywhere but the
approved merchant.

## Architecture

```
                 ┌──────────────┐
   buyer  ────▶  │   chat UI     │  (static/, product cards, approval chip)
                 └──────┬───────┘
                        │  /chat
                        ▼
          ┌─────────────────────────────┐
          │  FastAPI + Claude tool loop   │  (main.py)
          │  - budget guard                │
          │  - approval-gated tool calls   │
          └───┬─────────────┬──────────┬──┘
              │              │           │
              ▼              ▼           ▼
      ┌───────────────┐ ┌──────────┐ ┌───────────────────┐
      │ UCP catalog    │ │ Prava    │ │ Playwright         │
      │ client (ucp/)  │ │ mint     │ │ checkout            │
      │ search Shopify │ │ (prava/) │ │ (checkout/)         │
      │ store products │ │ one-time,│ │ runs the real       │
      │                │ │ capped   │ │ Shopify checkout    │
      │                │ │ card     │ │ with the minted card│
      └───────────────┘ └──────────┘ └───────────────────┘
```

## Setup

```bash
uv sync
uv run playwright install chromium
cp .env.example .env   # fill in ANTHROPIC_API_KEY and PRAVA_SECRET_KEY
uv run uvicorn main:app --reload
```

## Tests

```bash
uv run pytest -q
```

## Safety properties

- **Budget is enforced twice.** The agent's tool-calling loop checks the
  budget in code before it will act, and — independently — the card Prava
  mints is capped at the network level, so a compromised or misbehaving
  agent still cannot authorize a charge above the approved amount.
- **Merchant lock.** The minted card is scoped to the single approved
  merchant; a Playwright-side denylist also blocks navigation to any other
  checkout domain.
- **Credentials never reach the model.** Prava and Shopify credentials are
  loaded server-side only and are never included in any prompt, tool
  response, or context the model can see — the model requests actions, it
  never sees secrets.

## Pre-existing work disclosure

All code in this repository was written during the hackathon build window,
August 1–2, 2026. A teammate has a prior WhatsApp/agent gifting prototype
built before the hackathon; none of that prior prototype's code is used in
this repository. If that changes, it will be disclosed file-by-file in this
section.
