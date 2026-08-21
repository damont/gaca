import { useState, type FormEvent } from "react";
import { useAuth } from "../../context/AuthContext";
import { useRouter } from "../../hooks/useRouter";
import Field from "../common/Field";
import GoogleSignIn from "./GoogleSignIn";

export default function Register() {
  const { register, error } = useAuth();
  const { navigate } = useRouter();

  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setSubmitting(true);
    try {
      // Registering returns a token, so this signs you in as well — there is no
      // email verification step to wait on.
      await register(name, email, password);
    } catch {
      // message is already in `error` from AuthContext
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <main className="mx-auto flex min-h-dvh max-w-md flex-col justify-center gap-6 p-6">
      <h1 className="text-2xl font-semibold">Create an account</h1>

      <GoogleSignIn />

      <form onSubmit={onSubmit} className="flex flex-col gap-4">
        <Field
          id="name"
          label="Name"
          value={name}
          onChange={(e) => setName(e.target.value)}
          autoComplete="name"
          required
        />
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
          autoComplete="new-password"
          minLength={8}
          required
        />

        {error && <p className="text-sm text-danger">{error}</p>}

        <button
          type="submit"
          disabled={submitting}
          className="rounded-md bg-primary-600 px-4 py-2.5 font-medium text-white transition hover:bg-primary-700 disabled:opacity-60"
        >
          {submitting ? "Creating…" : "Create account"}
        </button>
      </form>

      <p className="text-sm text-text-secondary">
        Already have an account?{" "}
        <button
          onClick={() => navigate("/login")}
          className="font-medium text-primary-600 hover:underline"
        >
          Sign in
        </button>
      </p>
    </main>
  );
}
