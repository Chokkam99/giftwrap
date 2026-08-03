"use client";

import {
  Check,
  Copy,
  Fingerprint,
  Lock,
  MessageSquare,
  ShieldCheck,
} from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Separator } from "@/components/ui/separator";
import { formatPrice } from "@/lib/format";

/** `approve_payment` — the buyer hands off to Prava's hosted passkey flow.
 *  The card credential never comes back through the browser; the agent polls
 *  for it server-side, which is why the confirm button just sends a message. */
export function ApprovalPanel({
  iframeUrl,
  onConfirm,
  disabled,
}: {
  iframeUrl: string;
  onConfirm: () => void;
  disabled?: boolean;
}) {
  const [opened, setOpened] = useState(false);

  return (
    <div className="border-brand-border bg-brand-subtle/50 rounded-xl border p-4">
      <div className="flex items-center gap-2">
        <ShieldCheck className="text-brand size-4 shrink-0" />
        <p className="text-[13px] font-semibold">Approve with your passkey</p>
      </div>
      <p className="text-muted-foreground mt-1.5 text-[12.5px] leading-relaxed">
        This opens Prava&apos;s secure checkout in a new tab. Approve there,
        then confirm below. The card is minted for one merchant at one amount.
      </p>
      <div className="mt-3 flex flex-wrap gap-2">
        <Button
          size="sm"
          className="bg-brand text-brand-foreground hover:bg-brand/90"
          onClick={() => {
            window.open(iframeUrl, "_blank", "noopener,noreferrer");
            setOpened(true);
          }}
        >
          <Fingerprint className="size-3.5" />
          Approve with passkey
        </Button>
        <Button
          size="sm"
          variant="outline"
          disabled={disabled}
          onClick={onConfirm}
          className={opened ? undefined : "opacity-70"}
        >
          <Check className="size-3.5" />
          I&apos;ve approved it
        </Button>
      </div>
    </div>
  );
}

/** `receipt` — terminal success state for a completed checkout. */
export function ReceiptCard({
  orderId,
  amount,
  merchant,
}: {
  orderId: string | null;
  amount: string | null;
  merchant: string | null;
}) {
  const rows: Array<[string, string]> = [
    ["Order", orderId || "—"],
    ["Amount", amount ? formatPrice(amount, "INR") : "—"],
    ["Merchant", merchant || "—"],
  ];

  return (
    <div className="bg-card overflow-hidden rounded-xl border">
      <div className="flex items-center gap-2.5 px-4 py-3">
        <span className="bg-success/12 text-success flex size-8 items-center justify-center rounded-full">
          <Check className="size-4" strokeWidth={3} />
        </span>
        <div>
          <p className="text-[13px] font-semibold">Order confirmed</p>
          <p className="text-muted-foreground text-[11.5px]">
            The gift is on its way
          </p>
        </div>
      </div>
      <Separator />
      <dl className="divide-border divide-y">
        {rows.map(([label, value]) => (
          <div
            key={label}
            className="flex items-center justify-between px-4 py-2 text-[12.5px]"
          >
            <dt className="text-muted-foreground">{label}</dt>
            <dd className="tabular font-medium">{value}</dd>
          </div>
        ))}
      </dl>
    </div>
  );
}

/** `gift_link` — Mode B. The buyer shares a link and the recipient picks. */
export function GiftLinkCard({
  url,
  budget,
}: {
  url: string;
  budget: number | null;
}) {
  const [copied, setCopied] = useState(false);
  const fullUrl =
    typeof window !== "undefined" ? `${window.location.origin}${url}` : url;

  const shareText = `I sent you a gift. Pick anything you like${
    budget ? ` up to ${formatPrice(budget, "INR")}` : ""
  } — ${fullUrl}`;
  const encoded = encodeURIComponent(shareText);

  async function copy() {
    try {
      await navigator.clipboard.writeText(fullUrl);
      setCopied(true);
      toast.success("Link copied");
      setTimeout(() => setCopied(false), 1600);
    } catch {
      toast.error("Couldn't copy. Select the link and copy it manually.");
    }
  }

  return (
    <div className="bg-card rounded-xl border p-4">
      <p className="text-[13px] font-semibold">Your gift link is ready</p>
      <p className="text-muted-foreground mt-1 text-[12.5px] leading-relaxed">
        Share this so they can pick their own gift. The budget is enforced on
        our side, not theirs.
      </p>

      <div className="bg-muted/60 mt-3 flex items-center gap-2 rounded-lg px-3 py-2">
        <Lock className="text-muted-foreground size-3.5 shrink-0" />
        <code className="text-muted-foreground truncate font-mono text-[11.5px]">
          {fullUrl}
        </code>
      </div>

      <div className="mt-3 flex flex-wrap gap-2">
        <Button size="sm" variant="outline" asChild>
          <a
            href={`https://wa.me/?text=${encoded}`}
            target="_blank"
            rel="noopener noreferrer"
          >
            <MessageSquare className="size-3.5" />
            WhatsApp
          </a>
        </Button>
        <Button size="sm" variant="outline" asChild>
          <a href={`sms:&body=${encoded}`}>
            <MessageSquare className="size-3.5" />
            Messages
          </a>
        </Button>
        <Button size="sm" variant="outline" onClick={copy}>
          {copied ? <Check className="size-3.5" /> : <Copy className="size-3.5" />}
          {copied ? "Copied" : "Copy link"}
        </Button>
      </div>
    </div>
  );
}
