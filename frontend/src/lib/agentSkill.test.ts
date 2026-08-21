import { describe, expect, it } from "vitest";
import { agentSkill } from "./agentSkill";

const sample = {
  appName: "demo",
  baseUrl: "https://demo.example.com",
  description: "demo is a placeholder.",
  dataModel: "There is one Thing.",
  capabilities: ["do a thing"],
};

describe("agentSkill template", () => {
  it("uses <base> placeholders in discovery URLs (not the literal base)", () => {
    // Discovery URLs are templated so the agent treats them as substitutable
    // when the host has moved.
    const md = agentSkill(sample);
    expect(md).toContain("<base>/api/openapi.json");
    expect(md).toContain("<base>/api/agent");
  });

  it("strips a trailing slash from baseUrl in any literal mention", () => {
    const md = agentSkill({ ...sample, baseUrl: "https://demo.example.com/" });
    expect(md).not.toContain("https://demo.example.com//");
  });

  it("does not list any specific endpoint path", () => {
    const md = agentSkill(sample);
    expect(md).not.toMatch(/\/api\/(alerts|sessions|auth)\b/);
  });

  it("does not embed any token-shaped value", () => {
    const md = agentSkill(sample);
    expect(md).not.toMatch(/eyJ[A-Za-z0-9_-]{10,}/);
  });

  it("tells the agent what to ask the operator for", () => {
    // Setup-time framing: the guide must teach the agent how to onboard.
    const md = agentSkill(sample);
    expect(md).toContain("What you need before you can call anything");
    expect(md).toMatch(/ask the operator/i);
    expect(md).toContain("Authorization: Bearer");
  });

  it("includes the failure-modes table", () => {
    const md = agentSkill(sample);
    expect(md).toContain("| 401 |");
    expect(md).toContain("| 422 |");
  });

  it("includes capabilities when given, omits the section when not", () => {
    const withCaps = agentSkill(sample);
    expect(withCaps).toContain("do a thing");

    const { capabilities: _capabilities, ...withoutCaps } = sample;
    const md = agentSkill(withoutCaps);
    expect(md).not.toContain("Typical tasks an operator will give you");
  });
});
