import {
  useEffect,
  useLayoutEffect,
  useSyncExternalStore,
  type AnchorHTMLAttributes,
} from "react";

const changeEvent = "karakutu:navigate";
const scrollPositions = new Map<string, number>();
const sessionKey = Math.random().toString(36).slice(2);
let nextEntry = 0;

function entryKey() {
  const saved: unknown = window.history.state?.scrollKey;
  if (typeof saved === "string") return saved;
  const key = `${sessionKey}:${nextEntry++}`;
  window.history.replaceState(
    { ...window.history.state, scrollKey: key },
    "",
    window.location.href,
  );
  return key;
}

let navigation = {
  href: window.location.href,
  key: entryKey(),
  kind: "initial",
  newPage: true,
  scrollY: 0,
};
let restorationPending = false;
let restorationTimer: number | undefined;

function endRestoration() {
  restorationPending = false;
  window.clearTimeout(restorationTimer);
}

window.history.scrollRestoration = "manual";

function saveScroll() {
  if (restorationPending) return;
  scrollPositions.set(navigation.key, window.scrollY);
  window.history.replaceState(
    { ...window.history.state, scrollY: window.scrollY },
    "",
    window.location.href,
  );
}

// Track scroll in memory; history writes are reserved for navigation boundaries.
window.addEventListener("scroll", () => {
  if (!restorationPending) scrollPositions.set(navigation.key, window.scrollY);
}, { passive: true });
window.addEventListener("popstate", () => {
  const key = entryKey();
  const saved: unknown = scrollPositions.get(key) ?? window.history.state?.scrollY;
  navigation = {
    href: window.location.href,
    key,
    kind: "popstate",
    newPage: false,
    scrollY: typeof saved === "number" && Number.isFinite(saved) ? saved : 0,
  };
  endRestoration();
  restorationPending = true;
  // Expire even if a page never becomes ready; later content must not move the user.
  restorationTimer = window.setTimeout(endRestoration, 5000);
  window.dispatchEvent(new Event(changeEvent));
});

function subscribe(listener: () => void) {
  window.addEventListener(changeEvent, listener);
  return () => {
    window.removeEventListener(changeEvent, listener);
  };
}

// Each page reports readiness after its main content or terminal error is committed.
export function useScrollRestoration(ready: boolean) {
  const route = useRoute();
  useLayoutEffect(() => {
    if (!ready || !restorationPending || route.navigation.kind !== "popstate") return;
    window.scrollTo(0, route.navigation.scrollY);
    endRestoration();
  }, [ready, route.navigation]);
}

export function usePageNavigation() {
  const route = useRoute();
  useEffect(() => {
    if (!route.navigation.newPage || route.navigation.kind === "popstate") return;
    window.scrollTo(0, 0);
    document.getElementById("main")?.focus({ preventScroll: true });
  }, [route.navigation]);
}

export function navigate(
  to: string,
  {
    replace = false,
    state,
  }: { replace?: boolean; state?: unknown } = {},
) {
  const url = new URL(to, window.location.href);
  if (url.origin !== window.location.origin) {
    window.location.assign(url.href);
    return;
  }
  const newPage = url.pathname !== new URL(window.location.href).pathname;
  saveScroll();
  const nextState = state ?? (replace ? window.history.state : null);
  const key = replace ? navigation.key : `${sessionKey}:${nextEntry++}`;
  const scrollY = newPage ? 0 : window.scrollY;
  scrollPositions.set(key, scrollY);
  window.history[replace ? "replaceState" : "pushState"](
    { ...nextState, scrollKey: key, scrollY },
    "",
    url,
  );
  navigation = {
    href: url.href,
    key,
    kind: replace ? "replace" : "push",
    newPage,
    scrollY,
  };
  endRestoration();
  window.dispatchEvent(new Event(changeEvent));
}

export type RouteName =
  | "root"
  | "articles"
  | "article"
  | "runs"
  | "run"
  | "benchmarks"
  | "settings"
  | "not-found";

export function matchRoute(path: string): {
  name: RouteName;
  params: { id?: string; ref?: string };
} {
  if (path === "/") return { name: "root", params: {} };
  for (const name of ["articles", "runs", "benchmarks", "settings"] as const) {
    if (path === `/${name}`) return { name, params: {} };
  }
  const match = /^\/(articles|runs)\/([^/]+)$/.exec(path);
  if (match) {
    try {
      const value = decodeURIComponent(match[2]);
      return match[1] === "runs"
        ? { name: "run", params: { id: value } }
        : { name: "article", params: { ref: value } };
    } catch {
      /* Malformed addresses use the normal not-found page. */
    }
  }
  return { name: "not-found", params: {} };
}

export function useRoute() {
  const current = useSyncExternalStore(subscribe, () => navigation);
  const url = new URL(current.href);
  return {
    ...matchRoute(url.pathname),
    pathname: url.pathname,
    search: url.search,
    query: url.searchParams,
    navigation: current,
  };
}

export function Link({
  href = "/articles",
  onClick,
  state,
  ...props
}: AnchorHTMLAttributes<HTMLAnchorElement> & { state?: unknown }) {
  return (
    <a
      {...props}
      href={href}
      onClick={(event) => {
        onClick?.(event);
        if (
          event.defaultPrevented ||
          event.button !== 0 ||
          event.metaKey ||
          event.ctrlKey ||
          event.shiftKey ||
          event.altKey ||
          props.target !== undefined ||
          props.download !== undefined
        ) {
          return;
        }
        const url = new URL(href, window.location.href);
        if (url.origin !== window.location.origin) return;
        event.preventDefault();
        navigate(href, { state });
      }}
    />
  );
}
