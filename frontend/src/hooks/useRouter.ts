import { useState, useEffect, useCallback } from "react";

/**
 * Minimal URL routing, no react-router.
 *
 * The whole idea: keep the current path in React state, and keep that state in
 * sync with the browser's real URL. `navigate` pushes a history entry and
 * updates the state, so the address bar, the back button and deep links all
 * work without a routing library.
 *
 * Copied from toolshed — this file is generic and worth keeping identical
 * across the apps.
 */
interface RouterState {
  path: string;
  navigate: (path: string, replace?: boolean) => void;
}

export function useRouter(): RouterState {
  // The function form of useState runs only on the first render. Passing
  // window.location.pathname directly would re-read it on every render for no
  // reason.
  const [path, setPath] = useState(() => window.location.pathname);

  useEffect(() => {
    // The browser fires popstate on back/forward. Without this the URL would
    // change but React would keep rendering the old view.
    const onPopState = () => setPath(window.location.pathname);
    window.addEventListener("popstate", onPopState);
    // Returning a cleanup function is how an effect undoes itself — here,
    // removing the listener when the component goes away.
    return () => window.removeEventListener("popstate", onPopState);
  }, []);

  const navigate = useCallback((to: string, replace = false) => {
    if (to === window.location.pathname) return;
    if (replace) {
      window.history.replaceState({}, "", to);
    } else {
      window.history.pushState({}, "", to);
    }
    setPath(to);
    // pushState does NOT fire popstate, so any other useRouter instance would
    // still be showing the old path. Dispatching it manually syncs them all.
    window.dispatchEvent(new PopStateEvent("popstate"));
  }, []);

  return { path, navigate };
}

/** Match "/alerts/:id" against a real path, returning its params or null. */
export function matchPath(
  pattern: string,
  path: string,
): Record<string, string> | null {
  const patternParts = pattern.split("/").filter(Boolean);
  const pathParts = path.split("/").filter(Boolean);

  if (patternParts.length !== pathParts.length) return null;

  const params: Record<string, string> = {};
  for (let i = 0; i < patternParts.length; i++) {
    if (patternParts[i].startsWith(":")) {
      params[patternParts[i].slice(1)] = pathParts[i];
    } else if (patternParts[i] !== pathParts[i]) {
      return null;
    }
  }
  return params;
}
