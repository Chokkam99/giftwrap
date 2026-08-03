"use client";

import { Check, Gift, Loader2, Search, TriangleAlert } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { toast } from "sonner";

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
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import {
  ApiError,
  fetchGiftInfo,
  loadMoreGift,
  pickGift,
  searchGift,
} from "@/lib/api";
import { formatPrice, storeLabel } from "@/lib/format";
import type { GiftInfo, PickedProduct, Product } from "@/lib/types";
import { cn } from "@/lib/utils";

const QUICK_FILTERS = [
  { label: "All", query: "gift" },
  { label: "Jewellery", query: "jewelry" },
  { label: "Skincare", query: "skincare" },
  { label: "Self-care", query: "self care" },
  { label: "Accessories", query: "accessories" },
];

type Sort = "relevance" | "price-asc" | "price-desc";

export function GiftClient({ token }: { token: string }) {
  const [info, setInfo] = useState<GiftInfo | null>(null);
  const [fatal, setFatal] = useState<string | null>(null);
  const [picked, setPicked] = useState<PickedProduct | null>(null);

  const [products, setProducts] = useState<Product[]>([]);
  const [query, setQuery] = useState(QUICK_FILTERS[0].query);
  const [activeFilter, setActiveFilter] = useState(QUICK_FILTERS[0].label);
  const [store, setStore] = useState<string>("");
  const [sort, setSort] = useState<Sort>("relevance");
  // `searchKey` is what the fetching effect depends on. It only changes from
  // an event handler, which is also where `searching` is flipped on, so the
  // effect itself never sets state synchronously.
  const [searchKey, setSearchKey] = useState({ query: QUICK_FILTERS[0].query, store: "" });
  const [searching, setSearching] = useState(true);
  const [hasMore, setHasMore] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [pickingId, setPickingId] = useState<string | null>(null);
  const [detail, setDetail] = useState<ProductDetailRequest | null>(null);

  useEffect(() => {
    fetchGiftInfo(token)
      .then((data) => {
        setInfo(data);
        if (data.status === "picked" && data.picked_product) {
          setPicked(data.picked_product);
        }
      })
      .catch((err) =>
        setFatal(
          err instanceof ApiError && err.detail
            ? err.detail
            : "This link is invalid or has expired.",
        ),
      );
  }, [token]);

  /** Queue a new search. Called only from event handlers. */
  const requestSearch = useCallback((nextQuery: string, nextStore: string) => {
    setSearching(true);
    setSearchKey({ query: nextQuery, store: nextStore });
  }, []);

  useEffect(() => {
    if (!info || picked) return;
    let cancelled = false;

    searchGift(token, searchKey.query, searchKey.store || null)
      .then((data) => {
        if (cancelled) return;
        setProducts(data.products);
        setHasMore(Boolean(data.has_more));
        setSearching(false);
      })
      .catch(() => {
        if (cancelled) return;
        setProducts([]);
        setHasMore(false);
        setSearching(false);
        toast.error("Couldn't load gifts right now.");
      });

    return () => {
      cancelled = true;
    };
  }, [info, picked, token, searchKey]);

  const loadMore = useCallback(async () => {
    setLoadingMore(true);
    try {
      const data = await loadMoreGift(token);
      setProducts((p) => [...p, ...data.products]);
      setHasMore(Boolean(data.has_more));
      if (!data.products.length) toast.info("That's everything we found.");
    } catch {
      toast.error("Couldn't load more.");
    } finally {
      setLoadingMore(false);
    }
  }, [token]);

  const doPick = useCallback(
    async (product: Product) => {
      setPickingId(product.id);
      try {
        const res = await pickGift(token, {
          product_id: product.id,
          title: product.title ?? "Gift",
          price: product.price ?? "0",
        });
        if (res.ok && res.picked_product) {
          setPicked(res.picked_product);
        } else {
          toast.error(res.error ?? "Couldn't record your pick.");
        }
      } catch (err) {
        // The server re-validates the price against the buyer's budget. Its
        // refusal text is the useful message, not a generic failure.
        toast.error(
          err instanceof ApiError && err.detail
            ? err.detail
            : "Couldn't record your pick.",
        );
      } finally {
        setPickingId(null);
      }
    },
    [token],
  );

  const sorted = [...products].sort((a, b) => {
    if (sort === "price-asc") return Number(a.price) - Number(b.price);
    if (sort === "price-desc") return Number(b.price) - Number(a.price);
    return 0;
  });

  return (
    <div className="flex min-h-dvh flex-col">
      <header className="bg-background/85 sticky top-0 z-30 border-b backdrop-blur-md">
        <div className="mx-auto flex h-14 w-full max-w-5xl items-center justify-between gap-3 px-4">
          <BrandLockup title="GiftWrap" subtitle="Someone picked you" />
          <div className="flex items-center gap-1.5">
            {info && !picked ? (
              <Badge
                variant="outline"
                className="border-brand-border bg-brand-subtle text-brand tabular py-1 font-medium"
              >
                Up to {formatPrice(info.budget, info.currency)}
              </Badge>
            ) : null}
            <ThemeToggle />
          </div>
        </div>
      </header>

      <main className="mx-auto w-full max-w-5xl flex-1 px-4 pb-14">
        {fatal ? (
          <EmptyState
            icon={<TriangleAlert className="size-5" />}
            title="This link isn't working"
            body={fatal}
          />
        ) : picked ? (
          <PickedState picked={picked} currency={info?.currency ?? "INR"} />
        ) : !info ? (
          <div className="space-y-4 py-12">
            <Skeleton className="mx-auto h-6 w-64" />
            <Skeleton className="mx-auto h-4 w-80" />
          </div>
        ) : (
          <>
            <section className="animate-rise flex flex-col items-center py-10 text-center sm:py-14">
              <span className="bg-brand text-brand-foreground flex size-12 items-center justify-center rounded-2xl shadow-sm">
                <Gift className="size-6" />
              </span>
              <h1 className="mt-5 text-xl font-semibold tracking-tight text-balance sm:text-2xl">
                Someone sent you a gift
              </h1>
              {info.note ? (
                <p className="text-muted-foreground mt-3 max-w-md text-[14px] leading-relaxed text-balance italic">
                  &ldquo;{info.note}&rdquo;
                </p>
              ) : null}
              <p className="text-muted-foreground mt-4 text-[12.5px]">
                Pick anything up to{" "}
                <span className="text-foreground tabular font-medium">
                  {formatPrice(info.budget, info.currency)}
                </span>
                . They approve the payment.
              </p>
            </section>

            <div className="bg-background/85 sticky top-14 z-20 -mx-4 space-y-3 border-b px-4 py-3 backdrop-blur-md">
              <div className="flex flex-wrap items-center gap-2">
                {QUICK_FILTERS.map((f) => (
                  <Button
                    key={f.label}
                    size="sm"
                    variant={activeFilter === f.label ? "default" : "outline"}
                    className="h-7 rounded-full px-3 text-[12px] font-normal"
                    onClick={() => {
                      setActiveFilter(f.label);
                      setQuery(f.query);
                      requestSearch(f.query, store);
                    }}
                  >
                    {f.label}
                  </Button>
                ))}

                <div className="ml-auto flex items-center gap-2">
                  <Select
                    value={store || "all"}
                    onValueChange={(v) => {
                      const next = v === "all" ? "" : v;
                      setStore(next);
                      requestSearch(query, next);
                    }}
                  >
                    <SelectTrigger size="sm" className="h-7 w-[150px] text-[12px]">
                      <SelectValue placeholder="All stores" />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="all">All stores</SelectItem>
                      {info.stores.map((s) => (
                        <SelectItem key={s} value={s}>
                          {storeLabel(s)}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>

                  <Select
                    value={sort}
                    onValueChange={(v) => setSort(v as Sort)}
                  >
                    <SelectTrigger size="sm" className="h-7 w-[140px] text-[12px]">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="relevance">Relevance</SelectItem>
                      <SelectItem value="price-asc">Price: low to high</SelectItem>
                      <SelectItem value="price-desc">Price: high to low</SelectItem>
                    </SelectContent>
                  </Select>
                </div>
              </div>

              <form
                className="relative"
                onSubmit={(e) => {
                  e.preventDefault();
                  setActiveFilter("");
                  requestSearch(query, store);
                }}
              >
                <Search className="text-muted-foreground pointer-events-none absolute top-1/2 left-3 size-3.5 -translate-y-1/2" />
                <Input
                  value={query}
                  onChange={(e) => setQuery(e.target.value)}
                  placeholder="Search for something you'd love"
                  className="h-9 pl-9 text-[13px]"
                />
              </form>
            </div>

            {searching ? (
              <div className="grid grid-cols-2 gap-3 pt-5 sm:grid-cols-3 lg:grid-cols-4">
                {Array.from({ length: 8 }).map((_, i) => (
                  <div key={i} className="space-y-2">
                    <Skeleton className="aspect-square w-full rounded-xl" />
                    <Skeleton className="h-3 w-3/4" />
                    <Skeleton className="h-3 w-1/2" />
                  </div>
                ))}
              </div>
            ) : sorted.length === 0 ? (
              <EmptyState
                icon={<Search className="size-5" />}
                title="Nothing in budget for that"
                body="Try a different search, or switch stores."
              />
            ) : (
              <>
                <div className="grid grid-cols-2 gap-3 pt-5 sm:grid-cols-3 lg:grid-cols-4">
                  {sorted.map((p, i) => (
                    <ProductCard
                      key={`${p.id}-${i}`}
                      product={p}
                      index={i}
                      ctaLabel="This one!"
                      pending={pickingId === p.id}
                      className="w-full"
                      onSelect={doPick}
                      onOpenDetail={(prod) =>
                        setDetail({
                          product: prod,
                          budget: info.budget,
                          ctaLabel: "This one!",
                        })
                      }
                    />
                  ))}
                </div>

                {hasMore ? (
                  <div className="flex justify-center pt-6">
                    <Button
                      variant="outline"
                      size="sm"
                      disabled={loadingMore}
                      onClick={() => void loadMore()}
                    >
                      {loadingMore ? (
                        <Loader2 className="size-3.5 animate-spin" />
                      ) : null}
                      Load more
                    </Button>
                  </div>
                ) : null}
              </>
            )}
          </>
        )}
      </main>

      <ProductDetailModal
        request={detail}
        onOpenChange={(open) => !open && setDetail(null)}
        onApprove={doPick}
      />
    </div>
  );
}

function EmptyState({
  icon,
  title,
  body,
}: {
  icon: React.ReactNode;
  title: string;
  body: string;
}) {
  return (
    <div className="flex flex-col items-center py-20 text-center">
      <span className="bg-muted text-muted-foreground flex size-11 items-center justify-center rounded-full">
        {icon}
      </span>
      <p className="mt-4 text-[15px] font-semibold">{title}</p>
      <p className="text-muted-foreground mt-1 max-w-sm text-[13px] leading-relaxed">
        {body}
      </p>
    </div>
  );
}

function PickedState({
  picked,
  currency,
}: {
  picked: PickedProduct;
  currency: string;
}) {
  return (
    <div className={cn("animate-rise flex flex-col items-center py-20 text-center")}>
      <span className="bg-success/12 text-success flex size-14 items-center justify-center rounded-full">
        <Check className="size-7" strokeWidth={2.6} />
      </span>
      <h1 className="mt-5 text-xl font-semibold tracking-tight">Great choice</h1>
      <p className="mt-3 text-[14px] font-medium">{picked.title}</p>
      <p className="tabular text-muted-foreground mt-0.5 text-[13px]">
        {formatPrice(picked.price, currency)}
      </p>
      <p className="text-muted-foreground mt-6 max-w-sm text-[12.5px] leading-relaxed">
        It&apos;s on its way once your gifter approves the payment.
      </p>
    </div>
  );
}
