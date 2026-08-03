import { cn } from "@/lib/utils";

/** The wordmark glyph: a wrapped parcel. Used in the header, the empty state,
 *  and as the fallback for product images that fail to load. */
export function GiftMark({ className }: { className?: string }) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.7}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      className={cn("size-5", className)}
    >
      <rect x="3" y="9" width="18" height="12" rx="1.6" />
      <path d="M3 13h18" />
      <path d="M12 9v12" />
      <path d="M12 9C10.3 4.8 6 4.6 6 7.4 6 9 8 9 12 9Z" />
      <path d="M12 9c1.7-4.2 6-4.4 6-1.6C18 9 16 9 12 9Z" />
    </svg>
  );
}

/** The mark in its container, as it appears in both page headers. */
export function BrandLockup({
  title,
  subtitle,
}: {
  title: string;
  subtitle: string;
}) {
  return (
    <div className="flex items-center gap-3">
      <span className="flex size-9 shrink-0 items-center justify-center rounded-xl bg-brand text-brand-foreground shadow-sm">
        <GiftMark className="size-[18px]" />
      </span>
      <div className="min-w-0 leading-tight">
        <p className="truncate text-[15px] font-semibold tracking-tight">
          {title}
        </p>
        <p className="truncate text-xs text-muted-foreground">{subtitle}</p>
      </div>
    </div>
  );
}
