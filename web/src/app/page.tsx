"use client";

import { ArrowUp, Loader2, Lock, Sparkles } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { toast } from "sonner";

import {
  ApprovalPanel,
  GiftLinkCard,
  ReceiptCard,
} from "@/components/chat/action-panels";
import { Message, TypingIndicator } from "@/components/chat/message";
import { Welcome } from "@/components/chat/welcome";
import { BrandLockup } from "@/components/gift-mark";
import { ProductCard } from "@/components/product-card";
import {
  ProductDetailModal,
  type ProductDetailRequest,
} from "@/components/product-detail";
import { ThemeToggle } from "@/components/theme-toggle";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { ApiError, loadMoreChat, sendChat } from "@/lib/api";
import { formatPrice, makeConversationId, parseAmount } from "@/lib/format";
import type { ChatAction, Product, ProductSelection } from "@/lib/types";

interface Turn {
  id: number;
  role: "buyer" | "agent";
  text: string;
  cards?: Product[];
  hasMore?: boolean;
  sandboxItem?: Product | null;
  action?: ChatAction | null;
}

export default function BuyerChatPage() {
  const [turns, setTurns] = useState<Turn[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [budget, setBudget] = useState<number | null>(null);
  const [detail, setDetail] = useState<ProductDetailRequest | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);

  // Stable per-tab id, created once via a lazy initialiser. The value differs
  // between the prerender and the client, but it is never rendered into the
  // DOM (only sent in request bodies), so there is nothing to mismatch.
  const [conversationId] = useState(makeConversationId);

  const nextId = useRef(0);
  const scrollRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  const scrollToBottom = useCallback(() => {
    requestAnimationFrame(() => {
      const el = scrollRef.current;
      if (el) el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
    });
  }, []);

  useEffect(scrollToBottom, [turns, busy, scrollToBottom]);

  const send = useCallback(
    async (message: string, selection?: ProductSelection | null) => {
      const trimmed = message.trim();
      if (!trimmed || busy) return;

      setTurns((t) => [
        ...t,
        { id: nextId.current++, role: "buyer", text: trimmed },
      ]);
      setInput("");
      setBusy(true);

      try {
        const data = await sendChat(conversationId, trimmed, selection);
        const parsed = parseAmount(data.budget);
        if (parsed != null) setBudget(parsed);

        setTurns((t) => [
          ...t,
          {
            id: nextId.current++,
            role: "agent",
            text: data.reply,
            cards: data.cards ?? undefined,
            hasMore: data.has_more,
            sandboxItem: data.sandbox_checkout_item,
            action: data.action,
          },
        ]);
      } catch (err) {
        // Show the server's own refusal text when it sent one. A budget guard
        // or merchant-scope rejection is information, not a generic failure.
        toast.error(
          err instanceof ApiError && err.detail
            ? err.detail
            : "Couldn't reach the agent. Try again.",
        );
        setTurns((t) => [
          ...t,
          {
            id: nextId.current++,
            role: "agent",
            text: "Something went wrong reaching the agent. Please try again.",
          },
        ]);
      } finally {
        setBusy(false);
        inputRef.current?.focus();
      }
    },
    [busy, conversationId],
  );

  /** The sandbox card shows its catalog price but must mint for the verified
   *  full checkout total, which is what `payment_cap` carries. */
  const approve = useCallback(
    (product: Product) => {
      const amount = product.payment_cap ?? product.price;
      void send(
        `I'd like the ${product.title ?? "gift"} for ${formatPrice(
          amount,
          product.currency,
        )}.`,
        {
          id: product.id,
          title: product.title,
          price: amount,
          store: product.merchant,
          product_url: product.product_url,
        },
      );
    },
    [send],
  );

  const showMore = useCallback(
    async (turnId: number) => {
      setLoadingMore(true);
      try {
        const data = await loadMoreChat(conversationId);
        setTurns((t) =>
          t.map((turn) =>
            turn.id === turnId
              ? {
                  ...turn,
                  cards: [...(turn.cards ?? []), ...data.products],
                  hasMore: data.has_more,
                }
              : turn,
          ),
        );
        if (!data.products.length) {
          toast.info(data.message ?? "That's everything for this search.");
        }
      } catch {
        toast.error("Couldn't load more options.");
      } finally {
        setLoadingMore(false);
      }
    },
    [conversationId],
  );

  const isEmpty = turns.length === 0;

  const budgetChip = useMemo(() => {
    if (budget == null) return null;
    return (
      <Badge
        variant="outline"
        className="border-brand-border bg-brand-subtle text-brand gap-1.5 py-1 pr-2.5 pl-2 font-medium"
      >
        <Lock className="size-3" />
        <span className="tabular">{formatPrice(budget, "INR")}</span>
        <span className="hidden opacity-70 sm:inline">cap</span>
      </Badge>
    );
  }, [budget]);

  return (
    <div className="flex h-dvh flex-col">
      <header className="bg-background/85 sticky top-0 z-30 border-b backdrop-blur-md">
        <div className="mx-auto flex h-14 w-full max-w-3xl items-center justify-between gap-3 px-4">
          <BrandLockup title="GiftWrap" subtitle="Agentic gifting on Prava" />
          <div className="flex items-center gap-1.5">
            {budgetChip}
            <ThemeToggle />
          </div>
        </div>
      </header>

      <main ref={scrollRef} className="flex-1 overflow-y-auto overscroll-contain">
        <div className="mx-auto w-full max-w-3xl px-4 pb-6">
          {isEmpty ? (
            <Welcome onPick={(text) => void send(text)} />
          ) : (
            <div className="space-y-5 py-6">
              {turns.map((turn) => (
                <Message key={turn.id} role={turn.role} text={turn.text}>
                  {turn.cards?.length ? (
                    <div className="-mx-4 px-4">
                      <div className="no-scrollbar flex snap-x snap-mandatory gap-3 overflow-x-auto pb-1">
                        {turn.cards.map((card, i) => (
                          <ProductCard
                            key={`${card.id}-${i}`}
                            product={card}
                            index={i}
                            className="snap-start"
                            onSelect={approve}
                            onOpenDetail={(p) =>
                              setDetail({
                                product: p,
                                budget,
                                ctaLabel: "Gift this",
                              })
                            }
                          />
                        ))}
                      </div>
                    </div>
                  ) : null}

                  {turn.hasMore ? (
                    <Button
                      variant="ghost"
                      size="sm"
                      disabled={loadingMore}
                      onClick={() => void showMore(turn.id)}
                      className="text-muted-foreground h-7 rounded-full px-3 text-[12px]"
                    >
                      {loadingMore ? (
                        <Loader2 className="size-3 animate-spin" />
                      ) : (
                        <Sparkles className="size-3" />
                      )}
                      Show more like this
                    </Button>
                  ) : null}

                  {turn.sandboxItem ? (
                    <div className="border-brand-border/70 bg-brand-subtle/25 rounded-xl border border-dashed p-3">
                      <p className="text-[12.5px] font-medium">
                        Ready to try the payment flow?
                      </p>
                      <p className="text-muted-foreground mt-0.5 mb-3 text-[11.5px] leading-relaxed">
                        Catalog items above are for browsing. This verified
                        demo-store item is the safe sandbox checkout path.
                      </p>
                      <ProductCard
                        product={turn.sandboxItem}
                        ctaLabel="Try sandbox checkout"
                        onSelect={approve}
                      />
                    </div>
                  ) : null}

                  {turn.action?.type === "approve_payment" ? (
                    <ApprovalPanel
                      iframeUrl={turn.action.iframe_url}
                      disabled={busy}
                      onConfirm={() =>
                        void send("I completed the Prava approval")
                      }
                    />
                  ) : null}

                  {turn.action?.type === "receipt" ? (
                    <ReceiptCard
                      orderId={turn.action.order_id}
                      amount={turn.action.amount}
                      merchant={turn.action.merchant}
                    />
                  ) : null}

                  {turn.action?.type === "gift_link" ? (
                    <GiftLinkCard url={turn.action.url} budget={budget} />
                  ) : null}
                </Message>
              ))}

              {busy ? <TypingIndicator /> : null}
            </div>
          )}
        </div>
      </main>

      <div className="bg-background/85 border-t backdrop-blur-md">
        <form
          className="mx-auto w-full max-w-3xl px-4 py-3"
          onSubmit={(e) => {
            e.preventDefault();
            void send(input);
          }}
        >
          <div className="focus-within:border-foreground/25 focus-within:ring-ring/30 bg-card flex items-center gap-2 rounded-xl border py-1 pr-1 pl-3 transition focus-within:ring-[3px]">
            <Input
              ref={inputRef}
              value={input}
              disabled={busy}
              onChange={(e) => setInput(e.target.value)}
              placeholder="Who are you gifting, and what's the budget?"
              aria-label="Message"
              className="h-9 border-0 bg-transparent px-0 shadow-none focus-visible:ring-0 dark:bg-transparent"
            />
            <Button
              type="submit"
              size="icon"
              disabled={busy || !input.trim()}
              className="bg-brand text-brand-foreground hover:bg-brand/90 size-8 shrink-0 rounded-lg"
              aria-label="Send"
            >
              {busy ? (
                <Loader2 className="size-4 animate-spin" />
              ) : (
                <ArrowUp className="size-4" />
              )}
            </Button>
          </div>
          <p className="text-muted-foreground mt-2 text-center text-[11px]">
            Budget and merchant limits are enforced in code, then again by the
            card itself.
          </p>
        </form>
      </div>

      <ProductDetailModal
        request={detail}
        onOpenChange={(open) => !open && setDetail(null)}
        onApprove={approve}
      />
    </div>
  );
}
