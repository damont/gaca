import type { AgentTokenResponse, TokenResponse, User } from "../types";

/**
 * The one place the app talks to the API.
 *
 * API_BASE is a bare relative "/api" on purpose, in every environment: Vite's
 * dev proxy forwards it to :41700 locally, and nginx does the identical job in
 * production. No host is ever compiled in, so there is no per-environment base
 * URL to get wrong, and a build artifact is identical wherever it runs.
 */
const API_BASE = "/api";

class ApiClient {
  private token: string | null = null;

  /** Called by AuthContext on login, on logout, and on restore from storage. */
  setToken(token: string | null) {
    this.token = token;
  }

  private async request<T>(path: string, options: RequestInit = {}): Promise<T> {
    const headers: Record<string, string> = {
      ...(options.headers as Record<string, string>),
    };

    if (this.token) {
      headers["Authorization"] = `Bearer ${this.token}`;
    }

    if (!(options.body instanceof FormData)) {
      headers["Content-Type"] = "application/json";
    }

    const response = await fetch(`${API_BASE}${path}`, { ...options, headers });

    // fetch only rejects on network failure — a 401 or 500 still resolves. So
    // every non-2xx is turned into a thrown ApiError here, which means callers
    // can use try/catch and never have to remember to check response.ok.
    if (!response.ok) {
      const error = await response
        .json()
        .catch(() => ({ detail: response.statusText }));
      throw new ApiError(response.status, error.detail || "Request failed");
    }

    if (response.status === 204) return undefined as T;
    return response.json();
  }

  get<T>(path: string) {
    return this.request<T>(path);
  }

  post<T>(path: string, body?: unknown) {
    return this.request<T>(path, {
      method: "POST",
      body: body instanceof FormData ? body : JSON.stringify(body),
    });
  }

  put<T>(path: string, body?: unknown) {
    return this.request<T>(path, { method: "PUT", body: JSON.stringify(body) });
  }

  delete<T>(path: string) {
    return this.request<T>(path, { method: "DELETE" });
  }

  // ── auth ──────────────────────────────────────────────────────────

  /** Registering returns a token, not a user — signing up logs you in. */
  register(name: string, email: string, password: string) {
    return this.post<TokenResponse>("/auth/register", { name, email, password });
  }

  login(email: string, password: string) {
    return this.post<TokenResponse>("/auth/login", { email, password });
  }

  /**
   * Exchange a Google ID token for one of ours.
   *
   * The ID token is a JWT signed by Google, not by us — the API verifies its
   * signature and audience before trusting a word of it, then issues a normal
   * GACA token. Everything downstream sees an ordinary session.
   */
  googleLogin(idToken: string) {
    return this.post<TokenResponse>("/auth/google", { id_token: idToken });
  }

  me() {
    return this.get<User>("/auth/me");
  }

  /**
   * Mint a long-lived token for an agent.
   *
   * Authenticated by the session token already on this client — the API takes
   * no password here, because the caller is signed in and a stolen session can
   * already do everything the user can.
   */
  agentToken(expiresInDays: number) {
    return this.post<AgentTokenResponse>("/auth/agent-token", {
      expires_in_days: expiresInDays,
    });
  }
}

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

/** Shared instance — the token lives on it, so everything must use this one. */
export const api = new ApiClient();
