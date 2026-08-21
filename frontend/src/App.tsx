import { useEffect } from "react";
import { AuthProvider, useAuth } from "./context/AuthContext";
import { useRouter } from "./hooks/useRouter";
import LandingPage from "./components/auth/LandingPage";
import Login from "./components/auth/Login";
import Register from "./components/auth/Register";
import Profile from "./components/Profile";

/** Paths reachable without being signed in. */
const PUBLIC_PATHS = ["/", "/login", "/register"];

/**
 * Decides which screen to show, from two inputs: the URL, and whether anyone is
 * signed in.
 *
 * Split out from App so it sits *inside* AuthProvider — useAuth only works
 * below the provider, so a component that both provides and consumes context
 * would have to be two components anyway.
 */
function AuthGate() {
  const { user, loading } = useAuth();
  const { path, navigate } = useRouter();

  // Keep the URL honest about what's on screen. `replace` rather than push, so
  // the back button doesn't walk into a redirect loop.
  useEffect(() => {
    if (loading) return;
    if (user && PUBLIC_PATHS.includes(path)) {
      navigate("/profile", true);
    } else if (!user && !PUBLIC_PATHS.includes(path)) {
      navigate("/", true);
    }
  }, [loading, user, path, navigate]);

  // Until the stored token has been checked we don't know which way to go.
  // Rendering the landing page here would flash it at an already-signed-in user
  // on every reload.
  if (loading) {
    return (
      <div className="flex min-h-dvh items-center justify-center">
        <p className="text-sm text-text-muted">Loading…</p>
      </div>
    );
  }

  if (!user) {
    if (path === "/login") return <Login />;
    if (path === "/register") return <Register />;
    return <LandingPage />;
  }

  return <Profile />;
}

export default function App() {
  return (
    <AuthProvider>
      <AuthGate />
    </AuthProvider>
  );
}
