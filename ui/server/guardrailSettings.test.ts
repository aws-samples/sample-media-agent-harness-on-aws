import { describe, expect, it, vi } from "vitest";
import { listOptions, parseRef, readSettings, saveSelection } from "./guardrailSettings";
import type { ListGuardrails } from "./guardrailSettings";

const PATH = "/p-staging/runtime-arns";

// Two guardrails: one with published versions 1 and 2, one with only a DRAFT (never offered).
const list: ListGuardrails = async (input) => {
  if (!input.guardrailIdentifier) {
    return { guardrails: [
      { id: "abc123", name: "Strict", description: "brand safety", version: "DRAFT" },
      { id: "drafty", name: "Unpublished", version: "DRAFT" },
      { id: "arn:aws:bedrock:x", name: "bad id" },
    ] };
  }
  if (input.guardrailIdentifier === "abc123") return { guardrails: [{ version: "DRAFT" }, { version: "2" }, { version: "1" }] };
  return { guardrails: [{ version: "DRAFT" }] };
};

function fakeSsm(values: Record<string, string> = {}) {
  const puts: any[] = [];
  const call = vi.fn(async (target: string, body: any) => {
    if (target === "GetParameters") {
      return { Parameters: body.Names.filter((n: string) => n.split("/").pop()! in values)
        .map((n: string) => ({ Name: n, Value: values[n.split("/").pop()!] })) };
    }
    if (target === "PutParameter") { puts.push(body); values[body.Name.split("/").pop()!] = body.Value; return {}; }
    throw new Error(target);
  });
  return { call, puts };
}

describe("the platform guardrail setting", () => {
  it("parses ids and published versions only", () => {
    expect(parseRef(JSON.stringify({ id: "dflt1", version: "1", name: "d" }))).toEqual({ id: "dflt1", version: "1", name: "d" });
    expect(parseRef({ id: "arn:aws:bedrock:us-west-2:1:guardrail/x", version: "1" })).toBeNull();
    expect(parseRef({ id: "abc", version: "DRAFT" })).toBeNull();
    expect(parseRef("{bad")).toBeNull();
  });

  it("lists published versions, never a DRAFT-only guardrail or an ARN", async () => {
    expect(await listOptions(list)).toEqual([{ id: "abc123", name: "Strict", description: "brand safety", versions: ["1", "2"] }]);
  });

  it("applies the admin's pick, and none until there is one", async () => {
    const pick = { id: "abc123", version: "2", name: "Strict" };
    expect(await readSettings(fakeSsm({}).call, PATH)).toEqual({ effective: null });
    expect(await readSettings(fakeSsm({ GUARDRAIL_SELECTED: JSON.stringify(pick) }).call, PATH)).toEqual({ effective: pick });
    expect(await readSettings(fakeSsm({ GUARDRAIL_SELECTED: "none" }).call, PATH)).toEqual({ effective: null });
    expect(await readSettings(fakeSsm({ GUARDRAIL_SELECTED: "default" }).call, PATH)).toEqual({ effective: null });
  });

  it("saves a published version with its name, or none, and refuses anything else", async () => {
    const ssm = fakeSsm();
    expect(await saveSelection(ssm.call, list, PATH, { id: "abc123", version: "2" }))
      .toEqual({ ok: true, effective: { id: "abc123", version: "2", name: "Strict" } });
    expect(ssm.puts[0]).toEqual({ Name: `${PATH}/GUARDRAIL_SELECTED`, Value: JSON.stringify({ id: "abc123", version: "2", name: "Strict" }),
      Type: "String", Overwrite: true });
    expect(await saveSelection(ssm.call, list, PATH, { none: true })).toEqual({ ok: true, effective: null });
    expect(ssm.puts[1].Value).toBe("none");
    expect(await saveSelection(ssm.call, list, PATH, { id: "abc123", version: "9" })).toMatchObject({ ok: false, status: 404 });
    expect(await saveSelection(ssm.call, list, PATH, { id: "drafty", version: "1" })).toMatchObject({ ok: false, status: 404 });
    expect(await saveSelection(ssm.call, list, PATH, { id: "arn:x", version: "1" })).toMatchObject({ ok: false, status: 400 });
    expect(await saveSelection(ssm.call, list, PATH, {})).toMatchObject({ ok: false, status: 400 });
    expect(ssm.puts).toHaveLength(2);
  });
});
