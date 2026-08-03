"use client";

import { Search, ShieldCheck, Wallet } from "lucide-react";

import { GiftMark } from "@/components/gift-mark";
import { Button } from "@/components/ui/button";

const SUGGESTIONS = [
  "A birthday gift for my mom, ₹3000 budget",
  "Anniversary gift for my partner",
  "Something thoughtful for a close friend",
  "Let my sister pick her own, ₹2000",
];

const FEATURES = [
  { icon: Search, label: "Real products, searched live across six stores" },
  { icon: ShieldCheck, label: "Nothing is bought until you approve it" },
  { icon: Wallet, label: "One-time card, capped at your budget" },
];

export function Welcome({ onPick }: { onPick: (text: string) => void }) {
  return (
    <div className="animate-rise mx-auto flex max-w-lg flex-col items-center px-2 py-10 text-center sm:py-16">
      <span className="bg-brand text-brand-foreground flex size-12 items-center justify-center rounded-2xl shadow-sm">
        <GiftMark className="size-6" />
      </span>

      <h1 className="mt-5 text-xl font-semibold tracking-tight text-balance sm:text-2xl">
        Tell me who you&apos;re gifting, and the budget
      </h1>
      <p className="text-muted-foreground mt-2 max-w-md text-[13.5px] leading-relaxed text-balance">
        I&apos;ll find real products, you approve the one you like, and Prava
        mints a one-time card locked to that exact price and merchant. Nothing
        more can be charged.
      </p>

      <ul className="mt-6 w-full space-y-2 text-left">
        {FEATURES.map(({ icon: Icon, label }) => (
          <li
            key={label}
            className="text-muted-foreground flex items-center gap-2.5 text-[12.5px]"
          >
            <span className="bg-muted flex size-6 shrink-0 items-center justify-center rounded-md">
              <Icon className="size-3.5" />
            </span>
            {label}
          </li>
        ))}
      </ul>

      <div className="mt-7 flex flex-wrap justify-center gap-2">
        {SUGGESTIONS.map((text) => (
          <Button
            key={text}
            variant="outline"
            size="sm"
            className="text-muted-foreground hover:text-foreground h-auto rounded-full px-3 py-1.5 text-[12px] font-normal whitespace-normal"
            onClick={() => onPick(text)}
          >
            {text}
          </Button>
        ))}
      </div>
    </div>
  );
}
