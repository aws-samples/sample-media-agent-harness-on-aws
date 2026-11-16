// GET /models — the picker's list: the supported families (models.json `picker`, MODEL_PICKER) AND only models
// Bedrock lists as ACTIVE. Staging 2026-11-15: Sonnet 4.5 was LEGACY and Sonnet 4 / Nova Premier no longer listed
// at all (retired), while their inference profiles remained.
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import http from "http";

const h = vi.hoisted(() => ({ lifecycles: null as null | Error | Record<string, string> }));
vi.mock("@aws-sdk/client-bedrock", () => {
  class Cmd { input: any; constructor(i: any) { this.input = i; } }
  class ListInferenceProfilesCommand extends Cmd {}
  class ListFoundationModelsCommand extends Cmd {}
  class ListGuardrailsCommand extends Cmd {}
  const arn = (m: string) => [{ modelArn: `arn:aws:bedrock:us-west-2::foundation-model/${m}` }];
  class BedrockClient {
    async send(cmd: unknown) {
      if (cmd instanceof ListFoundationModelsCommand) {
        if (h.lifecycles instanceof Error) throw h.lifecycles;
        return { modelSummaries: Object.entries(h.lifecycles ?? {}).map(([modelId, status]) => ({ modelId, modelLifecycle: { status } })) };
      }
      if (cmd instanceof ListInferenceProfilesCommand) {
        return { inferenceProfileSummaries: [
          { inferenceProfileId: "us.anthropic.claude-sonnet-5-5", inferenceProfileName: "Sonnet 5.5", status: "ACTIVE", models: arn("anthropic.claude-sonnet-5-5") },
          { inferenceProfileId: "us.anthropic.claude-sonnet-4-5-20250929-v1:0", inferenceProfileName: "Sonnet 4.5", status: "ACTIVE", models: arn("anthropic.claude-sonnet-4-5-20250929-v1:0") },
          { inferenceProfileId: "us.anthropic.claude-sonnet-4-20250514-v1:0", inferenceProfileName: "Sonnet 4", status: "ACTIVE", models: arn("anthropic.claude-sonnet-4-20250514-v1:0") },
          { inferenceProfileId: "us.amazon.nova-pro-v1:0", inferenceProfileName: "Nova Pro", status: "ACTIVE", models: arn("amazon.nova-pro-v1:0") },
        ] };
      }
      return {};
    }
  }
  return { BedrockClient, ListInferenceProfilesCommand, ListFoundationModelsCommand, ListGuardrailsCommand };
});

import { buildApp } from "./app";
import { closeServers, listenLoopback } from "./testing/loopback";

const servers: http.Server[] = [];
const saved: Record<string, string | undefined> = {};
const VARS = ["AGENT_RUNTIME_ARN", "DEV_BYPASS_AUTH", "MODEL_PICKER"];
beforeEach(() => {
  for (const k of VARS) saved[k] = process.env[k];
  process.env.AGENT_RUNTIME_ARN = "arn:aws:bedrock-agentcore:us-west-2:000000000000:runtime/t-AAAAAAAAAA";
  process.env.DEV_BYPASS_AUTH = "true";
  process.env.MODEL_PICKER = JSON.stringify(["anthropic\\.claude-(?:opus|sonnet|haiku|fable)-(?!4-1)(?:4|[5-9])"]);
});
afterEach(async () => {
  await closeServers(servers);
  for (const k of VARS) { if (saved[k] === undefined) delete process.env[k]; else process.env[k] = saved[k]; }
});

const ids = async () => ((await (await fetch(`${await listenLoopback(buildApp(), servers)}/models`)).json()).models as { id: string }[]).map((m) => m.id);

describe("GET /models", () => {
  it("offers only supported models Bedrock lists as ACTIVE (no LEGACY, no retired)", async () => {
    h.lifecycles = { "anthropic.claude-sonnet-5-5": "ACTIVE", "anthropic.claude-sonnet-4-5-20250929-v1:0": "LEGACY", "amazon.nova-pro-v1:0": "ACTIVE" };
    expect(await ids()).toEqual(["us.anthropic.claude-sonnet-5-5"]);
  });
  it("unreadable lifecycles keep the supported list, never an empty picker", async () => {
    h.lifecycles = new Error("AccessDenied");
    expect(await ids()).toEqual(["us.anthropic.claude-sonnet-5-5", "us.anthropic.claude-sonnet-4-5-20250929-v1:0", "us.anthropic.claude-sonnet-4-20250514-v1:0"]);
  });
});
