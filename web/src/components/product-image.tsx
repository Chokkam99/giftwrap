"use client";

import { useState } from "react";

import { GiftMark } from "@/components/gift-mark";
import { cn } from "@/lib/utils";

/** Catalog images come from merchant CDNs we do not control, so a broken URL
 *  is routine rather than exceptional. Falling back to the wordmark keeps the
 *  card grid from collapsing into ragged heights. Plain <img> on purpose:
 *  next/image would need every current and future merchant CDN allow-listed,
 *  and a 404 there is a build-time-shaped problem at runtime. */
export function ProductImage({
  src,
  alt,
  className,
  iconClassName,
}: {
  src?: string | null;
  alt: string;
  className?: string;
  iconClassName?: string;
}) {
  const [failed, setFailed] = useState(false);

  if (!src || failed) {
    return (
      <div
        className={cn(
          "bg-muted text-muted-foreground/40 flex items-center justify-center",
          className,
        )}
      >
        <GiftMark className={cn("size-8", iconClassName)} />
      </div>
    );
  }

  return (
    // eslint-disable-next-line @next/next/no-img-element
    <img
      src={src}
      alt={alt}
      loading="lazy"
      onError={() => setFailed(true)}
      className={cn("h-full w-full object-cover", className)}
    />
  );
}
