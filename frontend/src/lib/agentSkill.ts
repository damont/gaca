/**
 * Generates the markdown "setup guide" behind the profile page's Download
 * skill button. See gaca-planning phase-10 notes: the audience is an agent at
 * SETUP time, not runtime — it says what the agent needs and where to look for
 * the rest (the OpenAPI schema), not which endpoints to call for which task.
 *
 * Kept framework-agnostic on purpose: no curl, no jq, no env var names. Claude
 * Code, Codex, openclaw, and a bare MCP server all read the same file.
 */
export interface AgentSkillInput {
  /** Lowercase app slug, used as the filename and in the title (e.g. "gaca"). */
  appName: string;
  /** Origin of the app, e.g. "https://gaca.example.com". */
  baseUrl: string;
  /** One paragraph: what the app is for. No endpoint names — describe purpose. */
  description: string;
  /** Short paragraph describing the data model in plain English. */
  dataModel: string;
  /** "Things this agent is typically asked to do" — individual tasks. */
  capabilities?: string[];
}

export function agentSkill(input: AgentSkillInput): string {
  const { appName, baseUrl, description, dataModel, capabilities = [] } = input;
  const base = baseUrl.replace(/\/$/, "");

  const capsBlock = capabilities.length
    ? `\n## Typical tasks an operator will give you\n\n${capabilities
        .map((c) => `- ${c}`)
        .join("\n")}\n`
    : "";

  return `# ${appName} — agent setup guide

You are an AI agent (Claude, Codex, openclaw, MCP server, custom script — doesn't
matter which) being asked to work with **${appName}**. This document is your
onboarding: it tells you what the app does, what you need from the operator
before you can talk to it, and how to find the rest on your own.

Read this once. If you're missing anything from "What you need" below, ask the
operator before doing anything else. Don't guess and don't retry.

## What this app is

${description}

${dataModel}

## What you need before you can call anything

Two pieces of information. Neither is encoded in this file.

1. **The app's base URL.** Where the API actually lives. The most recent value
   the app knew about is \`${base}\`, but the operator may have moved it
   (Cloudflare tunnel, different host, etc.) — confirm if you're unsure.

2. **A bearer token (JWT).** The operator mints this themselves from the
   profile page of the running app, under "Agent tokens". It's a long opaque
   string scoped to that operator's user account; it lives for a configurable
   1–365 days.

Store both however your runtime handles secrets (environment variable, MCP
config, secrets file, vault entry — your call). Never log the token, never
echo it back, never write it to a committed file.

### What to ask the operator if you're missing either

Be specific so they know exactly what to hand back:

> I need two things to start working with ${appName}: (1) the base URL where
> it's running, and (2) a bearer token from the "Agent tokens" section of your
> profile page on that app. Can you provide both?

## How to authenticate every request

One header on every call:

\`\`\`
Authorization: Bearer <the token>
\`\`\`

That's the whole auth model. No refresh tokens, no OAuth flow, no per-endpoint
scopes. Same token on every request until it expires.

## How to discover endpoints

**Don't assume endpoint paths.** The app publishes its full API surface as a
standard OpenAPI 3.x schema. Fetch this once at the start of a session and
work from it:

- Machine-readable schema: \`<base>/api/openapi.json\`
- Browsable Swagger UI: \`<base>/api/agent\`

To plan a task: pull the schema, search \`paths\` for endpoints relevant to
the task (by tag, path keyword, or method), read the request and response
schemas, match your payload exactly. If something stops working, re-read
the schema — it's the source of truth.

## Idiomatic behavior the app expects

- **Stay in your user.** Every resource is scoped to the authenticated user.
- **\`kind\` and other open-ended fields are not a closed enum.** New values
  appear without an API release; render/handle unknown ones generically rather
  than assuming you've seen the full set.
- **Respect your cadence.** If you've been scheduled to run daily, don't poll
  hourly without a reason. If hourly, don't burst.
${capsBlock}
## Status codes that aren't bugs

| Status | Meaning | What to do |
|---|---|---|
| 401 | Token missing, malformed, or expired | Stop. Ask the operator for a fresh one. Do not retry. |
| 403 | Authenticated but the action is forbidden for this user | Read the response body. Do not retry. |
| 404 | Resource doesn't exist or isn't yours | Don't retry; the ID is stale. |
| 409 | Conflict (usually a unique-key collision) | If your goal was idempotent insert, treat as success. |
| 422 | Payload doesn't match the schema | Re-read the schema for that path, fix the body, retry once. |
| 5xx | Server error | Backoff with jitter. Alert if it persists. |

## When you can't make progress

Surface specifics to the operator: which endpoint, what payload, what status,
what the response body said. Don't loop. The operator can mint a fresh token,
correct the base URL, or check server logs you can't see.

---

*Generated by ${appName} at \`${base}\`. The endpoint list is intentionally
not pinned here — fetch \`${base}/api/openapi.json\` for that.*
`;
}

/**
 * Triggers a browser download of `markdown` as `<appName>-agent.md`.
 *
 * Blob + object URL is the standard client-side "save this string as a file"
 * trick: wrap the text in a Blob, give it a temporary URL, click a hidden link
 * pointing at that URL, then revoke the URL so the browser can free it. There
 * is no server round-trip — the content already exists in memory.
 */
export function downloadSkill(appName: string, markdown: string) {
  const blob = new Blob([markdown], { type: "text/markdown;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `${appName}-agent.md`;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}
