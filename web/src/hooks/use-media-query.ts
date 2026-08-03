"use client";

import { useCallback, useSyncExternalStore } from "react";

/** Subscribes to a media query without setState-in-effect. `getServerSnapshot`
 *  reports false, so SSR and the hydrating render agree; React then swaps to
 *  the real match on the client. */
export function useMediaQuery(query: string): boolean {
  const subscribe = useCallback(
    (onChange: () => void) => {
      const mql = window.matchMedia(query);
      mql.addEventListener("change", onChange);
      return () => mql.removeEventListener("change", onChange);
    },
    [query],
  );

  const getSnapshot = useCallback(
    () => window.matchMedia(query).matches,
    [query],
  );

  return useSyncExternalStore(subscribe, getSnapshot, () => false);
}

const noopSubscribe = () => () => {};

/** True only after hydration. Same primitive, so it does not trip the
 *  set-state-in-effect rule the way a useState/useEffect pair does. */
export function useIsMounted(): boolean {
  return useSyncExternalStore(
    noopSubscribe,
    () => true,
    () => false,
  );
}
