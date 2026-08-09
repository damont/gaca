import { useEffect, useState } from "react";

type Health = { status: string; service: string; version: string };

/**
 * Scaffold check: prove the toolchain and the dev proxy both work.
 *
 * Replaced in 5c-5d by the real routed app. Right now its whole job is to make
 * one relative call to /api/health and render the result, which exercises the
 * two pieces that are easy to get quietly wrong: the build, and the proxy.
 */
export default function App() {
  // useState gives a component a value that survives re-renders, plus a setter.
  // Calling the setter is what tells React to render again with the new value —
  // assigning to a plain variable would change nothing on screen.
  const [health, setHealth] = useState<Health | null>(null);
  const [error, setError] = useState<string | null>(null);

  // useEffect runs code AFTER the component has rendered — the place for things
  // that touch the outside world. The [] at the end is the dependency list:
  // empty means "run once when this component first appears", rather than after
  // every render, which would fetch in an infinite loop.
  useEffect(() => {
    fetch("/api/health")
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then(setHealth)
      .catch((e: Error) => setError(e.message));
  }, []);

  // This is JSX: it looks like HTML but compiles to function calls that build a
  // description of the UI. className rather than class, because `class` is a
  // reserved word in JavaScript. {…} drops back into JavaScript.
  return (
    <main className="mx-auto flex min-h-dvh max-w-md flex-col justify-center gap-6 p-6">
      <header>
        <h1 className="text-2xl font-semibold text-text-primary">GACA</h1>
        <p className="text-sm text-text-secondary">
          Alerts from your agents, triaged by you.
        </p>
      </header>

      <section className="rounded-lg border border-border bg-surface-raised p-4">
        <h2 className="mb-2 text-xs font-medium uppercase tracking-wide text-text-muted">
          API connection
        </h2>
        {error !== null ? (
          <p className="text-sm text-danger">
            Can't reach the API — {error}. Is it running on 41700?
          </p>
        ) : health === null ? (
          <p className="text-sm text-text-muted">checking…</p>
        ) : (
          <p className="text-sm text-text-primary">
            <span className="text-success">●</span> {health.service} v{health.version} —{" "}
            {health.status}
          </p>
        )}
      </section>
    </main>
  );
}
