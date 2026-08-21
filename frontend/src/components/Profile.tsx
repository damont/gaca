import { useAuth } from "../context/AuthContext";
import AgentTokenSection from "./profile/AgentTokenSection";

/** The only signed-in screen. Everything this app does after sign-in lives here. */
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

      <AgentTokenSection />
    </main>
  );
}
