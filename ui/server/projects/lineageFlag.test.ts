// The asset-lineage deployment flag on the proxy (env-config `projects.lineageEdges`). CDK writes `"lineageEdges":
// true` into the proxy task's INGESTION_CONFIG only where the flag is on; `/config` reports it as `lineageEnabled`.
import { describe, expect, it } from "vitest";
import { readFileSync } from "fs";
import { resolve } from "path";

import { lineageFeatureEnabled } from "./access";

describe("lineageFeatureEnabled", () => {
  it("is on only for INGESTION_CONFIG.lineageEdges === true", () => {
    const env = (v?: string) => ({ ...(v === undefined ? {} : { INGESTION_CONFIG: v }) }) as NodeJS.ProcessEnv;
    expect(lineageFeatureEnabled(env(JSON.stringify({ frameIntervalSec: 1, lineageEdges: true })))).toBe(true);
    expect(lineageFeatureEnabled(env(JSON.stringify({ frameIntervalSec: 1 })))).toBe(false);
    expect(lineageFeatureEnabled(env(JSON.stringify({ lineageEdges: "true" })))).toBe(false);
    expect(lineageFeatureEnabled(env("{not json lineageEdges"))).toBe(false);
    expect(lineageFeatureEnabled(env())).toBe(false);
  });

  it("/config reports it as lineageEnabled (with Projects on)", () => {
    const app = readFileSync(resolve(__dirname, "../app.ts"), "utf8");
    expect(app).toContain("lineageEnabled: projectsEnabled() && lineageFeatureEnabled(),");
  });
});
