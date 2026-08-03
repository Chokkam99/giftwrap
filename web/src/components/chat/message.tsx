"use client";

import { Fragment, type ReactNode } from "react";

import { GiftMark } from "@/components/gift-mark";
import { cn } from "@/lib/utils";

/** Renders the same deliberately-small Markdown subset the original app.js
 *  allowed: **bold**, ordered lists and "-" bullets. Everything else stays a
 *  text node. Model output must never become HTML or a clickable link here;
 *  product cards and the detail modal own all navigation, so a prompt-injected
 *  product page cannot put a link in front of the buyer. */
function inline(text: string, keyPrefix: string): ReactNode[] {
  return text.split(/(\*\*[^*\n]+\*\*)/g).map((part, i) => {
    if (part.length > 4 && part.startsWith("**") && part.endsWith("**")) {
      return (
        <strong key={`${keyPrefix}-${i}`} className="font-semibold">
          {part.slice(2, -2)}
        </strong>
      );
    }
    return <Fragment key={`${keyPrefix}-${i}`}>{part}</Fragment>;
  });
}

function SafeMarkdown({ text }: { text: string }) {
  const lines = String(text ?? "").replace(/\r\n?/g, "\n").split("\n");
  const blocks: ReactNode[] = [];
  let i = 0;

  while (i < lines.length) {
    const orderedMatch = lines[i].match(/^\s*\d+\.\s+(.+)$/);
    const bulletMatch = lines[i].match(/^\s*-\s+(.+)$/);

    if (orderedMatch || bulletMatch) {
      const ordered = Boolean(orderedMatch);
      const items: string[] = [];
      while (i < lines.length) {
        const m = ordered
          ? lines[i].match(/^\s*\d+\.\s+(.+)$/)
          : lines[i].match(/^\s*-\s+(.+)$/);
        if (!m) break;
        items.push(m[1]);
        i += 1;
      }
      const ListTag = ordered ? "ol" : "ul";
      blocks.push(
        <ListTag
          key={`l-${i}`}
          className={cn(
            "my-1.5 space-y-1 pl-4",
            ordered ? "list-decimal" : "list-disc",
          )}
        >
          {items.map((item, n) => (
            <li key={n}>{inline(item, `li-${i}-${n}`)}</li>
          ))}
        </ListTag>,
      );
      continue;
    }

    if (lines[i] === "") {
      blocks.push(<div key={`sp-${i}`} className="h-2" />);
    } else {
      blocks.push(<p key={`p-${i}`}>{inline(lines[i], `p-${i}`)}</p>);
    }
    i += 1;
  }

  return <>{blocks}</>;
}

export function AgentAvatar() {
  return (
    <span className="bg-brand text-brand-foreground flex size-7 shrink-0 items-center justify-center rounded-lg">
      <GiftMark className="size-3.5" />
    </span>
  );
}

export function Message({
  role,
  text,
  children,
}: {
  role: "buyer" | "agent";
  text?: string;
  children?: ReactNode;
}) {
  if (role === "buyer") {
    return (
      <div className="animate-rise flex justify-end">
        <div className="bg-primary text-primary-foreground max-w-[85%] rounded-2xl rounded-br-md px-3.5 py-2 text-[13.5px] leading-relaxed sm:max-w-[70%]">
          {text}
        </div>
      </div>
    );
  }

  return (
    <div className="animate-rise flex gap-2.5">
      <AgentAvatar />
      <div className="min-w-0 flex-1 space-y-3">
        {text ? (
          <div className="text-[13.5px] leading-relaxed">
            <SafeMarkdown text={text} />
          </div>
        ) : null}
        {children}
      </div>
    </div>
  );
}

export function TypingIndicator() {
  return (
    <div className="flex gap-2.5">
      <AgentAvatar />
      <div className="flex items-center gap-1 pt-2.5">
        {[0, 1, 2].map((i) => (
          <span
            key={i}
            style={{ animationDelay: `${i * 160}ms` }}
            className="bg-muted-foreground/60 animate-blink size-1.5 rounded-full"
          />
        ))}
      </div>
    </div>
  );
}
