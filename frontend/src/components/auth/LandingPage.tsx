import { useRouter } from "../../hooks/useRouter";

export default function LandingPage() {
  const { navigate } = useRouter();

  return (
    <main className="mx-auto flex min-h-dvh max-w-md flex-col justify-center gap-8 p-6">
      <header className="space-y-2">
        <h1 className="text-3xl font-semibold">GACA</h1>
        <p className="text-text-secondary">
          Your agents notice things. This is where they tell you, and where you
          decide what happens next — hand it back, handle it, ignore it, or come
          back to it later.
        </p>
      </header>

      <div className="flex flex-col gap-3">
        <button
          onClick={() => navigate("/register")}
          className="rounded-md bg-primary-600 px-4 py-2.5 font-medium text-white transition hover:bg-primary-700"
        >
          Create an account
        </button>
        <button
          onClick={() => navigate("/login")}
          className="rounded-md border border-border px-4 py-2.5 font-medium text-text-primary transition hover:bg-surface-sunken"
        >
          Sign in
        </button>
      </div>

      <p className="text-sm text-text-muted">
        Triage happens in the Android app. This site is for signing up and
        issuing the key you hand your agent.
      </p>
    </main>
  );
}
