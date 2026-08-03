"use client";

import { ChevronLeft, ChevronRight } from "lucide-react";
import { useCallback, useEffect, useState } from "react";

import { ProductImage } from "@/components/product-image";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  Drawer,
  DrawerContent,
  DrawerDescription,
  DrawerHeader,
  DrawerTitle,
} from "@/components/ui/drawer";
import { Separator } from "@/components/ui/separator";
import { Skeleton } from "@/components/ui/skeleton";
import { useMediaQuery } from "@/hooks/use-media-query";
import { fetchProductDetail } from "@/lib/api";
import { formatPrice, storeLabel } from "@/lib/format";
import type { Product, ProductDetail as Detail } from "@/lib/types";
import { cn } from "@/lib/utils";

export interface ProductDetailRequest {
  product: Product;
  budget?: number | null;
  ctaLabel: string;
}

/** Shared detail view for both the buyer chat and the recipient grid.
 *  Renders as a centred Dialog on desktop and a bottom Drawer on touch, which
 *  is what the original modal.js hand-rolled as a swipe-to-dismiss sheet. */
export function ProductDetailModal({
  request,
  onOpenChange,
  onApprove,
}: {
  request: ProductDetailRequest | null;
  onOpenChange: (open: boolean) => void;
  onApprove: (product: Product) => void;
}) {
  const isDesktop = useMediaQuery("(min-width: 640px)");

  if (!request) return null;

  const title = request.product.title ?? "Untitled gift";

  // Keyed on the product id so opening a different product remounts with
  // clean state, instead of clearing it with setState inside an effect.
  const body = (
    <DetailBody
      key={request.product.id}
      request={request}
      onOpenChange={onOpenChange}
      onApprove={onApprove}
    />
  );

  if (isDesktop) {
    return (
      <Dialog open onOpenChange={onOpenChange}>
        <DialogContent className="gap-0 overflow-hidden p-0 sm:max-w-[480px]">
          <DialogHeader className="sr-only">
            <DialogTitle>{title}</DialogTitle>
            <DialogDescription>Product details</DialogDescription>
          </DialogHeader>
          {body}
        </DialogContent>
      </Dialog>
    );
  }

  return (
    <Drawer open onOpenChange={onOpenChange}>
      <DrawerContent className="max-h-[92vh] overflow-y-auto">
        <DrawerHeader className="sr-only">
          <DrawerTitle>{title}</DrawerTitle>
          <DrawerDescription>Product details</DrawerDescription>
        </DrawerHeader>
        {body}
      </DrawerContent>
    </Drawer>
  );
}

function DetailBody({
  request,
  onOpenChange,
  onApprove,
}: {
  request: ProductDetailRequest;
  onOpenChange: (open: boolean) => void;
  onApprove: (product: Product) => void;
}) {
  const { product, budget, ctaLabel } = request;

  // undefined = still loading, null = details unavailable but still giftable.
  const [detail, setDetail] = useState<Detail | null | undefined>(undefined);
  const [imageIndex, setImageIndex] = useState(0);
  const [variantIndex, setVariantIndex] = useState<number | null>(null);

  const store = product.merchant;
  const productId = product.id;

  useEffect(() => {
    if (!productId || !store) return;
    let cancelled = false;

    fetchProductDetail(store, productId, budget)
      .then((d) => {
        if (cancelled) return;
        setDetail(d);
        // The server already picked "cheapest in stock" as the headline
        // variant; preselect the chip that matches so the two agree.
        const idx = d.variants?.findIndex((v) => v.price === d.price);
        if (idx != null && idx >= 0) setVariantIndex(idx);
      })
      .catch(() => {
        if (!cancelled) setDetail(null);
      });

    return () => {
      cancelled = true;
    };
  }, [productId, store, budget]);

  const images = detail?.images?.length
    ? detail.images
    : product.image_url
      ? [product.image_url]
      : [];

  const variant = variantIndex == null ? undefined : detail?.variants?.[variantIndex];
  const currentPrice = variant?.price ?? detail?.price ?? product.price;
  const currency = variant?.currency ?? detail?.currency ?? product.currency;

  const step = useCallback(
    (delta: number) => {
      if (images.length < 2) return;
      setImageIndex((i) => (i + delta + images.length) % images.length);
    },
    [images.length],
  );

  const title = product.title ?? "Untitled gift";

  return (
    <div className="flex flex-col">
      <div className="bg-muted relative aspect-[4/3] w-full overflow-hidden sm:aspect-[16/10]">
        <ProductImage
          src={images[imageIndex]}
          alt={title}
          iconClassName="size-12"
          className="h-full w-full object-cover"
        />

        {images.length > 1 ? (
          <>
            <Button
              size="icon"
              variant="secondary"
              aria-label="Previous image"
              onClick={() => step(-1)}
              className="absolute top-1/2 left-2 size-8 -translate-y-1/2 rounded-full opacity-90 shadow-sm"
            >
              <ChevronLeft className="size-4" />
            </Button>
            <Button
              size="icon"
              variant="secondary"
              aria-label="Next image"
              onClick={() => step(1)}
              className="absolute top-1/2 right-2 size-8 -translate-y-1/2 rounded-full opacity-90 shadow-sm"
            >
              <ChevronRight className="size-4" />
            </Button>
            <div className="absolute bottom-2.5 left-1/2 flex -translate-x-1/2 gap-1.5">
              {images.map((_, i) => (
                <span
                  key={i}
                  className={cn(
                    "size-1.5 rounded-full transition-colors",
                    i === imageIndex ? "bg-foreground" : "bg-foreground/25",
                  )}
                />
              ))}
            </div>
          </>
        ) : null}
      </div>

      <div className="flex flex-col gap-3 p-5">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <h2 className="text-base leading-snug font-semibold">{title}</h2>
            {product.merchant ? (
              <p className="text-muted-foreground mt-0.5 text-xs">
                {storeLabel(product.merchant)}
              </p>
            ) : null}
          </div>
          <p className="tabular shrink-0 text-lg font-semibold">
            {formatPrice(currentPrice, currency)}
          </p>
        </div>

        {detail?.budget_headroom ? (
          <div className="bg-muted/60 flex items-center justify-between rounded-lg px-3 py-2 text-xs">
            <span className="text-muted-foreground">Leaves you</span>
            <span className="tabular font-medium">
              {formatPrice(
                detail.budget_headroom.remaining.toFixed(2),
                detail.budget_headroom.currency,
              )}{" "}
              <span className="text-muted-foreground font-normal">
                of{" "}
                {formatPrice(
                  detail.budget_headroom.budget.toFixed(2),
                  detail.budget_headroom.currency,
                )}
              </span>
            </span>
          </div>
        ) : null}

        {detail === undefined ? (
          <div className="space-y-2 pt-1">
            <Skeleton className="h-3 w-full" />
            <Skeleton className="h-3 w-[85%]" />
            <Skeleton className="h-3 w-[60%]" />
          </div>
        ) : detail === null ? (
          <p className="text-muted-foreground text-xs">
            Couldn&apos;t load full details, but you can still gift this.
          </p>
        ) : (
          <>
            {detail.description ? (
              <p className="text-muted-foreground line-clamp-5 text-[13px] leading-relaxed">
                {detail.description}
              </p>
            ) : null}

            {detail.variants?.length ? (
              <>
                <Separator className="my-1" />
                <div className="flex flex-wrap gap-1.5">
                  {detail.variants.map((v, i) => (
                    <Badge
                      key={v.id ?? v.label}
                      variant={i === variantIndex ? "default" : "outline"}
                      aria-disabled={!v.available}
                      onClick={() => v.available && setVariantIndex(i)}
                      className={cn(
                        "cursor-pointer px-2.5 py-1 text-xs font-normal",
                        !v.available && "cursor-not-allowed opacity-40 line-through",
                      )}
                    >
                      {v.label}
                    </Badge>
                  ))}
                </div>
              </>
            ) : null}
          </>
        )}

        <Button
          className="bg-brand text-brand-foreground hover:bg-brand/90 mt-2 w-full"
          onClick={() => {
            onOpenChange(false);
            onApprove({
              ...product,
              price: currentPrice ?? product.price,
              currency: currency ?? product.currency,
              variant_id: variant?.id ?? product.variant_id,
            });
          }}
        >
          {ctaLabel}
        </Button>
      </div>
    </div>
  );
}
