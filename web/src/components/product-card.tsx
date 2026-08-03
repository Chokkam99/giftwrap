"use client";

import { ShieldCheck } from "lucide-react";

import { ProductImage } from "@/components/product-image";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { formatPrice, storeLabel } from "@/lib/format";
import type { Product } from "@/lib/types";
import { cn } from "@/lib/utils";

export function ProductCard({
  product,
  index = 0,
  ctaLabel = "Gift this",
  onSelect,
  onOpenDetail,
  pending = false,
  className,
}: {
  product: Product;
  index?: number;
  ctaLabel?: string;
  onSelect: (product: Product) => void;
  /** Omitted for the sandbox card, which deliberately has one action only. */
  onOpenDetail?: (product: Product) => void;
  pending?: boolean;
  className?: string;
}) {
  const isSandbox = product.catalog_source === "sandbox_checkout";
  const clickable = Boolean(onOpenDetail) && !isSandbox;

  return (
    <article
      style={{ animationDelay: `${Math.min(index, 8) * 55}ms` }}
      className={cn(
        "animate-rise bg-card group relative flex w-[210px] shrink-0 flex-col overflow-hidden rounded-xl border text-left transition-shadow",
        clickable && "hover:border-foreground/15 cursor-pointer hover:shadow-md",
        isSandbox && "border-brand-border bg-brand-subtle/40 w-[236px]",
        className,
      )}
      onClick={clickable ? () => onOpenDetail?.(product) : undefined}
      role={clickable ? "button" : undefined}
      tabIndex={clickable ? 0 : undefined}
      onKeyDown={
        clickable
          ? (e) => {
              if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                onOpenDetail?.(product);
              }
            }
          : undefined
      }
    >
      <div className="bg-muted relative aspect-square overflow-hidden">
        <ProductImage
          src={product.image_url}
          alt={product.title ?? "Product image"}
          className={cn(
            "h-full w-full object-cover transition-transform duration-500",
            clickable && "group-hover:scale-[1.03]",
          )}
        />
        {isSandbox ? (
          <Badge
            variant="secondary"
            className="bg-brand text-brand-foreground absolute top-2 left-2 gap-1 border-transparent text-[10px] font-medium"
          >
            <ShieldCheck className="size-3" />
            Sandbox checkout
          </Badge>
        ) : null}
      </div>

      <div className="flex flex-1 flex-col gap-1 p-3">
        {product.merchant ? (
          <p className="text-muted-foreground truncate text-[11px] font-medium tracking-wide uppercase">
            {storeLabel(product.merchant)}
          </p>
        ) : null}

        <h3 className="line-clamp-2 text-[13px] leading-snug font-medium">
          {product.title ?? "Untitled gift"}
        </h3>

        <p className="tabular mt-0.5 text-[15px] font-semibold">
          {formatPrice(product.price, product.currency)}
        </p>

        {isSandbox && product.checkout_note ? (
          <p className="text-muted-foreground mt-1 text-[11px] leading-relaxed">
            {product.checkout_note}
          </p>
        ) : null}

        {isSandbox && product.payment_cap ? (
          <p className="text-brand tabular mt-1 text-[11px] font-medium">
            Card capped at {formatPrice(product.payment_cap, product.currency)}
          </p>
        ) : null}

        <Button
          size="sm"
          variant={isSandbox ? "default" : "secondary"}
          disabled={pending}
          className={cn("mt-3 w-full", isSandbox && "bg-brand text-brand-foreground hover:bg-brand/90")}
          onClick={(e) => {
            e.stopPropagation();
            onSelect(product);
          }}
        >
          {pending ? "Working…" : ctaLabel}
        </Button>
      </div>
    </article>
  );
}
