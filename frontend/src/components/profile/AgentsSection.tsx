import { useEffect, useState, type FormEvent } from "react";
import { api, ApiError } from "../../api/client";
import type { AgentsConfig } from "../../types";
import Field from "../common/Field";

/**
 * Where the operator points GACA at their own agents (Hermes profiles,
 * currently) — possibly more than one, sharing a single host.
 *
 * There is no shared, app-wide agent — every session and alert delegation for
 * this user resolves to one of the agents configured here. An agent's
 * `api_key` never comes back from the API once saved, same rule as a
 * password — editing a row means re-entering the key, not seeing the old one.
 */
export default function AgentsSection() {
  const [config, setConfig] = useState<AgentsConfig | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    api
      .agents()
      .then((result) => {
        if (!cancelled) setConfig(result);
      })
      .catch(() => {
        // Not fatal — the sections below still work, they just start blank.
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // Every mutation below returns the full AgentsConfig, so each handler just
  // replaces this component's state wholesale rather than patching one field.
  function applyError(err: unknown) {
    setError(err instanceof ApiError ? err.message : "Something went wrong");
  }

  if (loading) {
    return (
      <section className="rounded-lg border border-border bg-surface-raised p-4">
        <h2 className="text-sm font-medium">Your agents</h2>
        <p className="mt-1 text-sm text-text-muted">Loading…</p>
      </section>
    );
  }

  return (
    <section className="rounded-lg border border-border bg-surface-raised p-4">
      <h2 className="text-sm font-medium">Your agents</h2>
      <p className="mt-1 text-sm text-text-muted">
        Sessions and delegated alerts run against one of these — the default,
        unless something more specific applies.
      </p>

      {error && <p className="mt-2 text-sm text-danger">{error}</p>}

      <HostForm
        host={config?.host ?? null}
        onSaved={(updated) => {
          setConfig(updated);
          setError(null);
        }}
        onError={applyError}
      />

      <AgentList
        config={config}
        onChanged={(updated) => {
          setConfig(updated);
          setError(null);
        }}
        onError={applyError}
      />

      <AddAgentForm
        onAdded={(updated) => {
          setConfig(updated);
          setError(null);
        }}
        onError={applyError}
      />
    </section>
  );
}

function HostForm({
  host,
  onSaved,
  onError,
}: {
  host: string | null;
  onSaved: (config: AgentsConfig) => void;
  onError: (err: unknown) => void;
}) {
  const [value, setValue] = useState(host ?? "");
  const [saving, setSaving] = useState(false);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setSaving(true);
    try {
      onSaved(await api.setAgentHost(value));
    } catch (err) {
      onError(err);
    } finally {
      setSaving(false);
    }
  }

  return (
    <form onSubmit={onSubmit} className="mt-4 flex items-end gap-2">
      <div className="flex-1">
        <Field
          id="agent-host"
          label="Host"
          type="url"
          placeholder="http://your-hermes-box"
          value={value}
          onChange={(e) => setValue(e.target.value)}
          autoComplete="off"
          required
        />
      </div>
      <button
        type="submit"
        disabled={saving}
        className="rounded-md border border-border px-3 py-2 text-sm font-medium transition hover:bg-surface-sunken disabled:opacity-60"
      >
        {saving ? "Saving…" : "Save host"}
      </button>
    </form>
  );
}

/**
 * One row per configured agent. `agents.map(...)` turns the array into JSX
 * elements — each one needs a `key` so React can tell rows apart across
 * re-renders (it uses the name, since names are unique per user) instead of
 * re-matching them by position.
 */
function AgentList({
  config,
  onChanged,
  onError,
}: {
  config: AgentsConfig | null;
  onChanged: (config: AgentsConfig) => void;
  onError: (err: unknown) => void;
}) {
  // Which row (by name) has its edit form open — at most one at a time, so a
  // single string beats a flag per row.
  const [editing, setEditing] = useState<string | null>(null);

  if (!config || config.agents.length === 0) {
    return (
      <p className="mt-4 text-sm text-text-muted">
        No agents configured yet — add one below.
      </p>
    );
  }

  return (
    <ul className="mt-4 flex flex-col gap-2">
      {config.agents.map((agent) => (
        <li
          key={agent.name}
          className="rounded-md border border-border p-3 text-sm"
        >
          {editing === agent.name ? (
            <EditAgentForm
              name={agent.name}
              port={agent.port}
              onSaved={(updated) => {
                onChanged(updated);
                setEditing(null);
              }}
              onCancel={() => setEditing(null)}
              onError={onError}
            />
          ) : (
            <div className="flex items-center justify-between gap-3">
              <div>
                <span className="font-medium">{agent.name}</span>
                <span className="text-text-muted"> · port {agent.port}</span>
                {agent.is_default && (
                  <span className="ml-2 rounded-full bg-primary-100 px-2 py-0.5 text-xs font-medium text-primary-700">
                    default
                  </span>
                )}
              </div>
              <div className="flex shrink-0 gap-3">
                {!agent.is_default && (
                  <button
                    type="button"
                    className="text-text-secondary underline-offset-2 hover:underline"
                    onClick={() =>
                      api.setDefaultAgent(agent.name).then(onChanged).catch(onError)
                    }
                  >
                    Make default
                  </button>
                )}
                <button
                  type="button"
                  className="text-text-secondary underline-offset-2 hover:underline"
                  onClick={() => setEditing(agent.name)}
                >
                  Edit
                </button>
                <button
                  type="button"
                  className="text-danger underline-offset-2 hover:underline"
                  onClick={() =>
                    api.removeAgent(agent.name).then(onChanged).catch(onError)
                  }
                >
                  Remove
                </button>
              </div>
            </div>
          )}
        </li>
      ))}
    </ul>
  );
}

function EditAgentForm({
  name,
  port,
  onSaved,
  onCancel,
  onError,
}: {
  name: string;
  port: number;
  onSaved: (config: AgentsConfig) => void;
  onCancel: () => void;
  onError: (err: unknown) => void;
}) {
  const [portValue, setPortValue] = useState(String(port));
  const [apiKey, setApiKey] = useState("");
  const [saving, setSaving] = useState(false);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setSaving(true);
    try {
      onSaved(await api.updateAgent(name, Number(portValue), apiKey));
    } catch (err) {
      onError(err);
    } finally {
      setSaving(false);
    }
  }

  return (
    <form onSubmit={onSubmit} className="flex flex-col gap-3">
      <div className="flex items-center justify-between">
        <span className="font-medium">{name}</span>
        <button
          type="button"
          onClick={onCancel}
          className="text-xs text-text-muted hover:underline"
        >
          Cancel
        </button>
      </div>
      <Field
        id={`agent-port-${name}`}
        label="Port"
        type="number"
        min={1}
        max={65535}
        value={portValue}
        onChange={(e) => setPortValue(e.target.value)}
        required
      />
      <Field
        id={`agent-key-${name}`}
        label="API key"
        type="password"
        placeholder="re-enter to change"
        value={apiKey}
        onChange={(e) => setApiKey(e.target.value)}
        autoComplete="off"
        required
      />
      <button
        type="submit"
        disabled={saving}
        className="self-start rounded-md bg-primary-600 px-3 py-1.5 text-sm font-medium text-white transition hover:bg-primary-700 disabled:opacity-60"
      >
        {saving ? "Saving…" : "Save"}
      </button>
    </form>
  );
}

function AddAgentForm({
  onAdded,
  onError,
}: {
  onAdded: (config: AgentsConfig) => void;
  onError: (err: unknown) => void;
}) {
  const [name, setName] = useState("");
  const [port, setPort] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [saving, setSaving] = useState(false);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setSaving(true);
    try {
      const updated = await api.addAgent(name, Number(port), apiKey);
      onAdded(updated);
      setName("");
      setPort("");
      setApiKey(""); // never held in state longer than it takes to send it
    } catch (err) {
      onError(err);
    } finally {
      setSaving(false);
    }
  }

  return (
    <form onSubmit={onSubmit} className="mt-4 flex flex-col gap-3 border-t border-border pt-4">
      <h3 className="text-sm font-medium">Add an agent</h3>
      <Field
        id="new-agent-name"
        label="Name"
        placeholder="toolshed"
        value={name}
        onChange={(e) => setName(e.target.value)}
        autoComplete="off"
        required
      />
      <Field
        id="new-agent-port"
        label="Port"
        type="number"
        min={1}
        max={65535}
        value={port}
        onChange={(e) => setPort(e.target.value)}
        required
      />
      <Field
        id="new-agent-key"
        label="API key"
        type="password"
        value={apiKey}
        onChange={(e) => setApiKey(e.target.value)}
        autoComplete="off"
        required
      />
      <button
        type="submit"
        disabled={saving}
        className="self-start rounded-md bg-primary-600 px-4 py-2 font-medium text-white transition hover:bg-primary-700 disabled:opacity-60"
      >
        {saving ? "Adding…" : "Add agent"}
      </button>
    </form>
  );
}
