/**
 * Shapes the API actually returns. These mirror `gaca_api/schemas/auth.py`.
 *
 * Only what the web UI uses lives here. Alert shapes are deliberately absent:
 * triage belongs to the Android app, and this UI exists to sign up and mint a
 * key. `api-contract.md` is the canonical description of the Alert shape.
 */

/** GET /api/auth/me — mirrors UserResponse. */
export interface User {
  id: string;
  name: string;
  email: string;
}

/** POST /api/auth/register and /login — mirrors TokenResponse. */
export interface TokenResponse {
  access_token: string;
  token_type: string;
}

/** POST /api/auth/agent-token — a longer-lived token, otherwise identical. */
export interface AgentTokenResponse extends TokenResponse {
  expires_in_days: number;
}
