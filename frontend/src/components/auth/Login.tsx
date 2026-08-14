import { useState, type FormEvent } from "react";
import { useAuth } from "../../context/AuthContext";
import { useRouter } from "../../hooks/useRouter";
import Field from "../common/Field";
import GoogleSignIn from "./GoogleSignIn";

export default function Login() {
  const { login, error } = useAuth();
  const { navigate } = useRouter();

  // Controlled inputs: React owns the value, and the input only changes because
  // onChange sets state and the component re-renders. The DOM never holds a
  // value React doesn't know about, so `email` here is always what's on screen.
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);

  async function onSubmit(e: FormEvent) {
    // Without this the browser does a full page navigation and reloads the app.
    e.preventDefault();
    setSubmitting(true);
    try {
      await login(email, password);
      // No navigate() on success — setting the user re-renders AuthGate, which
      // swaps this screen out on its own.
    } catch {
      // AuthContext already put the message in `error`; nothing to do here but
      // let the button come back to life in `finally`.
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <main className="mx-auto flex min-h-dvh max-w-md flex-col justify-center gap-6 p-6">
      <h1 className="text-2xl font-semibold">Sign in</h1>

      <GoogleSignIn />

      <form onSubmit={onSubmit} className="flex flex-col gap-4">
        <Field
          id="email"
          label="Email"
          type="email"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          autoComplete="email"
          required
        />
        <Field
          id="password"
          label="Password"
          type="password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          autoComplete="current-password"
          required
        />

        {error && <p className="text-sm text-danger">{error}</p>}

        <button
          type="submit"
          disabled={submitting}
          className="rounded-md bg-primary-600 px-4 py-2.5 font-medium text-white transition hover:bg-primary-700 disabled:opacity-60"
        >
          {submitting ? "Signing in…" : "Sign in"}
        </button>
      </form>

      <p className="text-sm text-text-secondary">
        No account?{" "}
        <button
          onClick={() => navigate("/register")}
          className="font-medium text-primary-600 hover:underline"
        >
          Create one
        </button>
      </p>
    </main>
  );
}
