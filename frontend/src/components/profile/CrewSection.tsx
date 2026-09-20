import { useEffect, useState, type FormEvent } from "react";
import { api, ApiError } from "../../api/client";
import type { CrewConfig } from "../../types";
import Field from "../common/Field";

/**
 * The same 6 muted hues gaca-api auto-assigns from (models/user.py's
 * CREW_COLOR_PALETTE) — kept here only as swatch options for the picker; the
 * server is the source of truth for what a crew member's color actually is.
 */
const COLOR_PALETTE = [
  "#4E7093",
  "#6B8F5A",
  "#B97A56",
  "#8A6A9E",
  "#3F8E93",
  "#B0923F",
];

/**
 * Two phases, matching the API: connect to your one Hermes agent first
 * (`POST /agent/connect` — sets the host and creates a "default" crew
 * member in one step, so there's already someone to chat with), then
 * optionally add more crew members (more Hermes profiles) on top of it.
 *
 * There is no shared, app-wide agent — every session and alert delegation
 * for this user resolves to one of the crew members configured here. A
 * member's `api_key` never comes back from the API once saved, same rule as
 * a password — editing a row means re-entering the key, not seeing the old
 * one.
 */
export default function CrewSection() {
  const [config, setConfig] = useState<CrewConfig | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    api
      .crew()
      .then((result) => {
        if (!cancelled) setConfig(result);
      })
      .catch(() => {
        // Not fatal — treated the same as "not connected yet."
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // Every mutation below returns the full CrewConfig, so each handler just
  // replaces this component's state wholesale rather than patching one field.
  function applyError(err: unknown) {
    setError(err instanceof ApiError ? err.message : "Something went wrong");
  }

  function applyUpdate(updated: CrewConfig) {
    setConfig(updated);
    setError(null);
  }

  if (loading) {
    return (
      <section className="rounded-lg border border-border bg-surface-raised p-4">
        <h2 className="text-sm font-medium">Your agent</h2>
        <p className="mt-1 text-sm text-text-muted">Loading…</p>
      </section>
    );
  }

  const connected = config?.host != null;

  return (
    <section className="rounded-lg border border-border bg-surface-raised p-4">
      <h2 className="text-sm font-medium">{connected ? "Your crew" : "Your agent"}</h2>
      <p className="mt-1 text-sm text-text-muted">
        {connected
          ? "Sessions and delegated alerts run against one of these — the default, unless something more specific applies."
          : "Connect the Hermes agent GACA should talk to. This creates your first crew member too, so there's already someone to chat with."}
      </p>

      {error && <p className="mt-2 text-sm text-danger">{error}</p>}

      {connected ? (
        <>
          <AgentHost host={config!.host!} onSaved={applyUpdate} onError={applyError} />
          <CrewList config={config} onChanged={applyUpdate} onError={applyError} />
          <AddCrewMemberForm onAdded={applyUpdate} onError={applyError} />
        </>
      ) : (
        <ConnectAgentForm onConnected={applyUpdate} onError={applyError} />
      )}
    </section>
  );
}

function ConnectAgentForm({
  onConnected,
  onError,
}: {
  onConnected: (config: CrewConfig) => void;
  onError: (err: unknown) => void;
}) {
  const [host, setHost] = useState("");
  const [port, setPort] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [saving, setSaving] = useState(false);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setSaving(true);
    try {
      onConnected(await api.connectAgent(host, Number(port), apiKey));
    } catch (err) {
      onError(err);
    } finally {
      setSaving(false);
    }
  }

  return (
    <form onSubmit={onSubmit} className="mt-4 flex flex-col gap-3">
      <Field
        id="agent-host"
        label="Host"
        type="url"
        placeholder="http://your-hermes-box"
        value={host}
        onChange={(e) => setHost(e.target.value)}
        autoComplete="off"
        required
      />
      <Field
        id="agent-port"
        label="Port"
        type="number"
        min={1}
        max={65535}
        value={port}
        onChange={(e) => setPort(e.target.value)}
        required
      />
      <Field
        id="agent-key"
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
        {saving ? "Connecting…" : "Connect"}
      </button>
    </form>
  );
}

/** Shown once already connected — the host as plain text, with a "change"
 * link that reveals the edit form. Moving the box is rare enough that it
 * doesn't deserve the same prominence as the initial connect step. */
function AgentHost({
  host,
  onSaved,
  onError,
}: {
  host: string;
  onSaved: (config: CrewConfig) => void;
  onError: (err: unknown) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState(host);
  const [saving, setSaving] = useState(false);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setSaving(true);
    try {
      onSaved(await api.updateAgentHost(value));
      setEditing(false);
    } catch (err) {
      onError(err);
    } finally {
      setSaving(false);
    }
  }

  if (!editing) {
    return (
      <p className="mt-4 text-sm text-text-secondary">
        Agent host: <span className="font-mono">{host}</span>{" "}
        <button
          type="button"
          className="text-text-muted underline-offset-2 hover:underline"
          onClick={() => setEditing(true)}
        >
          Change
        </button>
      </p>
    );
  }

  return (
    <form onSubmit={onSubmit} className="mt-4 flex items-end gap-2">
      <div className="flex-1">
        <Field
          id="agent-host-edit"
          label="Host"
          type="url"
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
        {saving ? "Saving…" : "Save"}
      </button>
      <button
        type="button"
        onClick={() => setEditing(false)}
        className="text-sm text-text-muted hover:underline"
      >
        Cancel
      </button>
    </form>
  );
}

/** A small filled circle in a crew member's own color — the same visual
 * anchor the Android app uses (avatar, chat accent, bubbles): one glance
 * tells you which crew member a row belongs to without reading the name. */
function ColorDot({ color }: { color: string }) {
  return (
    <span
      className="inline-block h-2.5 w-2.5 rounded-full align-middle"
      style={{ backgroundColor: color }}
      aria-hidden
    />
  );
}

/**
 * One row per configured crew member. `crew.map(...)` turns the array into
 * JSX elements — each one needs a `key` so React can tell rows apart across
 * re-renders (it uses the name, since names are unique per user) instead of
 * re-matching them by position.
 */
function CrewList({
  config,
  onChanged,
  onError,
}: {
  config: CrewConfig | null;
  onChanged: (config: CrewConfig) => void;
  onError: (err: unknown) => void;
}) {
  // Which row (by name) has its edit form open — at most one at a time, so a
  // single string beats a flag per row.
  const [editing, setEditing] = useState<string | null>(null);

  if (!config || config.crew.length === 0) {
    return (
      <p className="mt-4 text-sm text-text-muted">
        No crew configured yet — add one below.
      </p>
    );
  }

  return (
    <ul className="mt-4 flex flex-col gap-2">
      {config.crew.map((member) => (
        <li
          key={member.name}
          className="rounded-md border border-border p-3 text-sm"
          style={{ borderLeft: `3px solid ${member.color}` }}
        >
          {editing === member.name ? (
            <EditCrewMemberForm
              name={member.name}
              port={member.port}
              color={member.color}
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
                <ColorDot color={member.color} />
                <span className="ml-2 font-medium">{member.name}</span>
                <span className="text-text-muted"> · port {member.port}</span>
                {member.is_default && (
                  <span className="ml-2 rounded-full bg-primary-100 px-2 py-0.5 text-xs font-medium text-primary-700">
                    default
                  </span>
                )}
              </div>
              <div className="flex shrink-0 gap-3">
                {!member.is_default && (
                  <button
                    type="button"
                    className="text-text-secondary underline-offset-2 hover:underline"
                    onClick={() =>
                      api
                        .setDefaultCrewMember(member.name)
                        .then(onChanged)
                        .catch(onError)
                    }
                  >
                    Make default
                  </button>
                )}
                <button
                  type="button"
                  className="text-text-secondary underline-offset-2 hover:underline"
                  onClick={() => setEditing(member.name)}
                >
                  Edit
                </button>
                <button
                  type="button"
                  className="text-danger underline-offset-2 hover:underline"
                  onClick={() =>
                    api.removeCrewMember(member.name).then(onChanged).catch(onError)
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

/** A row of clickable swatches plus the currently-chosen one. Shared by the
 * add and edit forms so the two never drift apart in behavior. */
function ColorPicker({
  value,
  onChange,
}: {
  value: string;
  onChange: (color: string) => void;
}) {
  return (
    <div>
      <span className="mb-1 block text-sm font-medium text-text-secondary">
        Color
      </span>
      <div className="flex gap-2">
        {COLOR_PALETTE.map((color) => (
          <button
            key={color}
            type="button"
            aria-label={`Use ${color}`}
            onClick={() => onChange(color)}
            className="h-6 w-6 rounded-full transition"
            style={{
              backgroundColor: color,
              outline: value === color ? "2px solid currentColor" : "none",
              outlineOffset: 2,
            }}
          />
        ))}
      </div>
    </div>
  );
}

function EditCrewMemberForm({
  name,
  port,
  color,
  onSaved,
  onCancel,
  onError,
}: {
  name: string;
  port: number;
  color: string;
  onSaved: (config: CrewConfig) => void;
  onCancel: () => void;
  onError: (err: unknown) => void;
}) {
  const [portValue, setPortValue] = useState(String(port));
  const [apiKey, setApiKey] = useState("");
  const [colorValue, setColorValue] = useState(color);
  const [saving, setSaving] = useState(false);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setSaving(true);
    try {
      onSaved(
        await api.updateCrewMember(name, Number(portValue), apiKey, colorValue),
      );
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
        id={`crew-port-${name}`}
        label="Port"
        type="number"
        min={1}
        max={65535}
        value={portValue}
        onChange={(e) => setPortValue(e.target.value)}
        required
      />
      <Field
        id={`crew-key-${name}`}
        label="API key"
        type="password"
        placeholder="re-enter to change"
        value={apiKey}
        onChange={(e) => setApiKey(e.target.value)}
        autoComplete="off"
        required
      />
      <ColorPicker value={colorValue} onChange={setColorValue} />
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

function AddCrewMemberForm({
  onAdded,
  onError,
}: {
  onAdded: (config: CrewConfig) => void;
  onError: (err: unknown) => void;
}) {
  const [name, setName] = useState("");
  const [port, setPort] = useState("");
  const [apiKey, setApiKey] = useState("");
  // No color picked yet — the server auto-assigns one from the palette
  // (first swatch not already in use) when this stays undefined.
  const [color, setColor] = useState<string | undefined>(undefined);
  const [saving, setSaving] = useState(false);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setSaving(true);
    try {
      const updated = await api.addCrewMember(name, Number(port), apiKey, color);
      onAdded(updated);
      setName("");
      setPort("");
      setApiKey(""); // never held in state longer than it takes to send it
      setColor(undefined);
    } catch (err) {
      onError(err);
    } finally {
      setSaving(false);
    }
  }

  return (
    <form onSubmit={onSubmit} className="mt-4 flex flex-col gap-3 border-t border-border pt-4">
      <h3 className="text-sm font-medium">Add a crew member</h3>
      <Field
        id="new-crew-name"
        label="Name"
        placeholder="toolshed"
        value={name}
        onChange={(e) => setName(e.target.value)}
        autoComplete="off"
        required
      />
      <Field
        id="new-crew-port"
        label="Port"
        type="number"
        min={1}
        max={65535}
        value={port}
        onChange={(e) => setPort(e.target.value)}
        required
      />
      <Field
        id="new-crew-key"
        label="API key"
        type="password"
        value={apiKey}
        onChange={(e) => setApiKey(e.target.value)}
        autoComplete="off"
        required
      />
      <ColorPicker value={color ?? ""} onChange={setColor} />
      <button
        type="submit"
        disabled={saving}
        className="self-start rounded-md bg-primary-600 px-4 py-2 font-medium text-white transition hover:bg-primary-700 disabled:opacity-60"
      >
        {saving ? "Adding…" : "Add crew member"}
      </button>
    </form>
  );
}
