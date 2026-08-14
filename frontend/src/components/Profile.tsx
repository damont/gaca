import { useAuth } from "../context/AuthContext";

/**
 * The only signed-in screen. Everything this app does after sign-in lives here.
 *
 * 5e adds the "Agent tokens" section — the duration picker, the one-time token
 * display, and the downloadable agent setup guide.
 */
export default function Profile() {
  const { user, logout } = useAuth();

  return (
    <main className="mx-auto flex min-h-dvh max-w-lg flex-col gap-6 p-6">
      <header className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold">{user?.name}</h1>
          <p className="text-sm text-text-secondary">{user?.email}</p>
        </div>
        <button
          onClick={logout}
          className="rounded-md border border-border px-3 py-1.5 text-sm font-medium transition hover:bg-surface-sunken"
        >
          Sign out
        </button>
      </header>

      <section className="rounded-lg border border-border bg-surface-raised p-4">
        <h2 className="mb-1 text-sm font-medium">Agent tokens</h2>
        <p className="text-sm text-text-muted">Coming in the next step.</p>
      </section>
    </main>
  );
}
