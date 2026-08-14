import {
  createContext,
  useContext,
  useState,
  useEffect,
  useCallback,
  type ReactNode,
} from "react";
import { api, ApiError } from "../api/client";
import type { User } from "../types";

/**
 * Who is signed in, available anywhere without threading props down the tree.
 *
 * Context is React's answer to "many components need this one value". The
 * provider holds the state; any descendant calls useAuth() to read it. Without
 * it, the user object would have to be passed through every intermediate
 * component that doesn't care about it.
 *
 * Simpler than toolshed's: no Google sign-in, no invites, and registering
 * returns a token, so signing up logs you straight in rather than parking you
 * on a "check your email" screen.
 */
interface AuthState {
  user: User | null;
  loading: boolean;
  error: string | null;
}

interface AuthContextValue extends AuthState {
  login: (email: string, password: string) => Promise<void>;
  register: (name: string, email: string, password: string) => Promise<void>;
  logout: () => void;
}

const AuthContext = createContext<AuthContextValue | null>(null);

// localStorage survives a reload and a closed tab, which is what makes the
// session persist. It is readable by any script on this origin — acceptable
// here because the token is the credential either way, and the alternative
// (an httpOnly cookie) needs CSRF handling the API doesn't have.
const TOKEN_KEY = "gaca_token";

export function AuthProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<AuthState>({
    user: null,
    loading: true,
    error: null,
  });

  // Restore a session on first load: a stored token is only a claim, so we ask
  // the API who it belongs to. An expired or revoked token fails here and gets
  // cleared, which is why `loading` starts true — until this resolves we don't
  // know whether to show the app or the landing page.
  const fetchUser = useCallback(async () => {
    const token = localStorage.getItem(TOKEN_KEY);
    if (!token) {
      setState({ user: null, loading: false, error: null });
      return;
    }

    api.setToken(token);
    try {
      const user = await api.me();
      setState({ user, loading: false, error: null });
    } catch {
      localStorage.removeItem(TOKEN_KEY);
      api.setToken(null);
      setState({ user: null, loading: false, error: null });
    }
  }, []);

  useEffect(() => {
    fetchUser();
  }, [fetchUser]);

  const setTokenAndFetch = async (accessToken: string) => {
    localStorage.setItem(TOKEN_KEY, accessToken);
    api.setToken(accessToken);
    const user = await api.me();
    setState({ user, loading: false, error: null });
  };

  const login = async (email: string, password: string) => {
    setState((s) => ({ ...s, error: null }));
    try {
      const { access_token } = await api.login(email, password);
      await setTokenAndFetch(access_token);
    } catch (err) {
      const message = err instanceof ApiError ? err.message : "Login failed";
      setState((s) => ({ ...s, error: message }));
      // Re-thrown so the form can stop its spinner; the message is already in
      // context, so the form doesn't have to handle it twice.
      throw err;
    }
  };

  const register = async (name: string, email: string, password: string) => {
    setState((s) => ({ ...s, error: null }));
    try {
      const { access_token } = await api.register(name, email, password);
      await setTokenAndFetch(access_token);
    } catch (err) {
      const message =
        err instanceof ApiError ? err.message : "Registration failed";
      setState((s) => ({ ...s, error: message }));
      throw err;
    }
  };

  const logout = () => {
    // Purely client-side: the token stays valid until it expires. There is no
    // server-side revocation — rotating GACA_JWT_SECRET is the only one, and it
    // signs everybody out.
    localStorage.removeItem(TOKEN_KEY);
    api.setToken(null);
    setState({ user: null, loading: false, error: null });
  };

  return (
    <AuthContext.Provider value={{ ...state, login, register, logout }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within AuthProvider");
  return ctx;
}
