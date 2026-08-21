import { useState } from "react";
import { api, ApiError } from "../../api/client";
import { agentSkill, downloadSkill } from "../../lib/agentSkill";

const DURATIONS = [1, 7, 30, 90, 365];

const APP_NAME = "gaca";

const DESCRIPTION =
  "GACA is a personal home-alerting system. An AI agent (Hermes) running on " +
  "your machines posts alerts here — disk space, failed jobs, anything worth " +
  "a human's attention — and the operator triages them from an Android app. " +
  "This web UI exists to sign in and mint the tokens an agent needs to talk " +
  "to the API; alert triage itself lives in the Android app.";

const DATA_MODEL =
  "The core resource is the Alert: a source, an open-ended `kind` (e.g. " +
  "`disk.low`), a `severity` of `info`/`warn`/`urgent`, a title and body, an " +
  "arbitrary `data` payload, and an optional `dedupe_key` so posting the same " +
  "condition repeatedly doesn't spam duplicates — reuse the key and the API " +
  "folds it into the existing alert instead of creating a new one. Alerts move " +
  "through five triage states: new, snoozed, delegated, acted, dismissed. " +
  "`suggested_actions` on an alert are prompts the operator can hand back to " +
  "an agent; delegating an alert opens a Session, the run that does the work.";

const CAPABILITIES = [
  "Post a new alert when something worth a human's attention happens (disk full, job failed, unusual activity)",
  "Reuse the same dedupe_key for a recurring condition instead of posting duplicates",
  "Attach suggested_actions so the operator can one-tap delegate follow-up work back to you",
];

/**
 * The profile page's "Agent tokens" section: discovery links, a duration
 * picker that mints a long-lived JWT, and the Download skill button.
 *
 * The minted token is held only in this component's state — never written to
 * localStorage, never logged. Dismissing it (or navigating away, which unmounts
 * this component) is the only "revocation" there is client-side; the token
 * itself stays valid server-side until it expires.
 */
export default function AgentTokenSection() {
  const [days, setDays] = useState(365);
  const [token, setToken] = useState<string | null>(null);
  const [generating, setGenerating] = useState(false);
  const [copied, setCopied] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const origin = window.location.origin;

  async function onGenerate() {
    setGenerating(true);
    setError(null);
    try {
      const response = await api.agentToken(days);
      setToken(response.access_token);
      setCopied(false);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not generate a token");
    } finally {
      setGenerating(false);
    }
  }

  async function onCopy() {
    if (!token) return;
    await navigator.clipboard.writeText(token);
    setCopied(true);
  }

  function onDownloadSkill() {
    const markdown = agentSkill({
      appName: APP_NAME,
      baseUrl: origin,
      description: DESCRIPTION,
      dataModel: DATA_MODEL,
      capabilities: CAPABILITIES,
    });
    downloadSkill(APP_NAME, markdown);
  }

  return (
    <section className="rounded-lg border border-border bg-surface-raised p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <h2 className="text-sm font-medium">Agent tokens</h2>
        <button
          onClick={onDownloadSkill}
          className="rounded-md border border-border px-3 py-1.5 text-sm font-medium transition hover:bg-surface-sunken"
        >
          Download skill
        </button>
      </div>

      {/* Always visible, even before a token is minted — an agent (or you,
          debugging) can look at the schema without signing anything first. */}
      <dl className="mt-3 flex flex-col gap-1 text-sm text-text-secondary">
        <div className="flex gap-2">
          <dt className="text-text-muted">API base</dt>
          <dd className="font-mono">{origin}/api</dd>
        </div>
        <div className="flex gap-2">
          <dt className="text-text-muted">Schema</dt>
          <dd className="font-mono">{origin}/api/openapi.json</dd>
        </div>
        <div className="flex gap-2">
          <dt className="text-text-muted">Swagger UI</dt>
          <dd className="font-mono">{origin}/api/agent</dd>
        </div>
      </dl>

      <div className="mt-4 flex flex-wrap items-end gap-3">
        <label className="block">
          <span className="mb-1 block text-sm font-medium text-text-secondary">
            Expires in
          </span>
          <select
            value={days}
            onChange={(e) => setDays(Number(e.target.value))}
            className="rounded-md border border-border bg-surface px-3 py-2 text-text-primary outline-none focus:border-primary-500 focus:ring-2 focus:ring-primary-100"
          >
            {DURATIONS.map((d) => (
              <option key={d} value={d}>
                {d} day{d === 1 ? "" : "s"}
              </option>
            ))}
          </select>
        </label>

        <button
          onClick={onGenerate}
          disabled={generating}
          className="rounded-md bg-primary-600 px-4 py-2 font-medium text-white transition hover:bg-primary-700 disabled:opacity-60"
        >
          {generating ? "Generating…" : "Generate"}
        </button>
      </div>

      {error && <p className="mt-2 text-sm text-danger">{error}</p>}

      {/* Shown once. There is no "view it again" — closing this clears it from
          state, and nothing has written it anywhere else. */}
      {token && (
        <div className="mt-4 flex flex-col gap-2 rounded-md border border-border bg-surface p-3">
          <p className="text-sm text-text-secondary">
            Copy this now — it won't be shown again. Hand it to your agent
            alongside the skill file.
          </p>
          <textarea
            readOnly
            value={token}
            rows={4}
            className="w-full resize-none overflow-y-auto rounded-md border border-border bg-surface-sunken p-2 font-mono text-xs text-text-primary outline-none"
            onFocus={(e) => e.currentTarget.select()}
          />
          <div className="flex gap-2">
            <button
              onClick={onCopy}
              className="rounded-md border border-border px-3 py-1.5 text-sm font-medium transition hover:bg-surface-sunken"
            >
              {copied ? "Copied" : "Copy to clipboard"}
            </button>
            <button
              onClick={() => setToken(null)}
              className="rounded-md border border-border px-3 py-1.5 text-sm font-medium transition hover:bg-surface-sunken"
            >
              Dismiss
            </button>
          </div>
        </div>
      )}
    </section>
  );
}
