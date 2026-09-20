import type { CrewConfig, AgentTokenResponse, TokenResponse, User } from "../types";

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
   * Mint a long-lived token for an external agent (Hermes, a script, an AI
   * coding tool) to call this API with — unrelated to the user's own crew
   * below: this is a credential for something to reach *gaca-api*, not for
   * gaca-api to reach a Hermes box.
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

  /**
   * This user's own crew — there is no shared, app-wide one. Every mutation
   * below returns the full CrewConfig, so the caller can just replace its
   * state wholesale after any change.
   */
  crew() {
    return this.get<CrewConfig>("/crew");
  }

  /**
   * First-time setup: connect this account to its one Hermes agent. Creates
   * `name` (default "default") as the first crew member from `port`/
   * `apiKey`, so there's already someone to chat with. Only valid once —
   * the API 422s a second call; use `updateAgentHost` to move to a new box.
   */
  connectAgent(host: string, port: number, apiKey: string, name?: string) {
    return this.post<CrewConfig>("/agent/connect", {
      host,
      port,
      api_key: apiKey,
      name,
    });
  }

  /** Move the already-connected agent (and every crew member under it) to a
   * new host. Leaves the crew list itself untouched. */
  updateAgentHost(host: string) {
    return this.put<CrewConfig>("/agent", { host });
  }

  addCrewMember(name: string, port: number, apiKey: string, color?: string) {
    return this.post<CrewConfig>("/crew", {
      name,
      port,
      api_key: apiKey,
      color,
    });
  }

  updateCrewMember(name: string, port: number, apiKey: string, color?: string) {
    return this.put<CrewConfig>(`/crew/${encodeURIComponent(name)}`, {
      port,
      api_key: apiKey,
      color,
    });
  }

  removeCrewMember(name: string) {
    return this.delete<CrewConfig>(`/crew/${encodeURIComponent(name)}`);
  }

  setDefaultCrewMember(name: string) {
    return this.post<CrewConfig>(
      `/crew/${encodeURIComponent(name)}/default`,
    );
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
