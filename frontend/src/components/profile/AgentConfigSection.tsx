import { useEffect, useState, type FormEvent } from "react";
import { api, ApiError } from "../../api/client";
import Field from "../common/Field";

/**
 * Where the operator points GACA at their own agent (Hermes, currently).
 *
 * There is no shared, app-wide agent — every session and alert delegation for
 * this user resolves to exactly the base URL and key submitted here. Nobody
 * else's account can reach it. `agent_api_key` never comes back from the API
 * once saved, same rule as a password — this section shows "configured" and
 * the base URL, never the key.
 */
export default function AgentConfigSection() {
  const [baseUrl, setBaseUrl] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [configured, setConfigured] = useState(false);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    api
      .agentConfig()
      .then((config) => {
        if (cancelled) return;
        setBaseUrl(config.agent_base_url ?? "");
        setConfigured(config.agent_configured);
      })
      .catch(() => {
        // Not fatal — the form still works, it just starts blank.
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setSaving(true);
    setError(null);
    setSaved(false);
    try {
      const result = await api.setAgentConfig(baseUrl, apiKey);
      setConfigured(result.agent_configured);
      setApiKey(""); // never held in state longer than it takes to send it
      setSaved(true);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not save agent config");
    } finally {
      setSaving(false);
    }
  }

  return (
    <section className="rounded-lg border border-border bg-surface-raised p-4">
      <h2 className="text-sm font-medium">Your agent</h2>
      <p className="mt-1 text-sm text-text-muted">
        {loading
          ? "Loading…"
          : configured
            ? "Configured — sessions and delegated alerts run against this agent."
            : "Not configured yet. Delegating an alert or starting a chat will fail until you set this."}
      </p>

      <form onSubmit={onSubmit} className="mt-4 flex flex-col gap-4">
        <Field
          id="agent-base-url"
          label="Agent base URL"
          type="url"
          placeholder="http://your-hermes-box:8642"
          value={baseUrl}
          onChange={(e) => setBaseUrl(e.target.value)}
          autoComplete="off"
          required
        />
        <Field
          id="agent-api-key"
          label="Agent API key"
          type="password"
          value={apiKey}
          onChange={(e) => setApiKey(e.target.value)}
          autoComplete="off"
          required
        />
        {configured && (
          <p className="-mt-2 text-xs text-text-muted">
            The API is never able to show you the saved key again, so it has
            to be re-entered here any time you update the URL too — both
            fields save together.
          </p>
        )}

        {error && <p className="text-sm text-danger">{error}</p>}
        {saved && !error && (
          <p className="text-sm text-text-secondary">Saved.</p>
        )}

        <button
          type="submit"
          disabled={saving}
          className="self-start rounded-md bg-primary-600 px-4 py-2 font-medium text-white transition hover:bg-primary-700 disabled:opacity-60"
        >
          {saving ? "Saving…" : "Save"}
        </button>
      </form>
    </section>
  );
}
