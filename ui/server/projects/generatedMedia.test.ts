import { describe, expect, it, vi } from "vitest";
import { buildTurnMessage, externalRefsIn, makeGeneratedMediaNotifier, type TurnEnd } from "./generatedMedia";

const H = "0123456789abcdef01234567";
const base: TurnEnd = {
  sessionId: "11111111-2222-4333-8444-555555555555",
  turnId: "t1",
  projectId: "p" + "a".repeat(32),
  actorSub: "user-1",
  startedAt: 1_000,
  endedAt: 2_000,
  text: "",
  steps: [],
  summary: null,
};

describe("externalRefsIn", () => {
  it("finds CDN, presigned, and bare references, unique and sorted", () => {
    const refs = externalRefsIn(
      `https://cdn.example/artifacts/external/${H}.mp4 and artifacts/external/${H}.mp4`,
      `https://b.s3.amazonaws.com/artifacts/external/${H}.png?X-Amz-Signature=x`,
      `not this: artifacts/external/${H}ff.mp4`,
    );
    expect(refs).toEqual([`artifacts/external/${H}.mp4`, `artifacts/external/${H}.png`]);
  });
});

describe("buildTurnMessage", () => {
  it("carries the turn, its window, and refs from text, steps, and summary", () => {
    const msg = buildTurnMessage({
      ...base,
      text: `here: https://cdn/artifacts/external/${H}.mp4`,
      steps: [{ type: "step", label: `rendered artifacts/external/${"b".repeat(24)}.png` }],
    });
    expect(msg).toEqual({
      kind: "turn", sessionId: base.sessionId, turnId: "t1", projectId: base.projectId, actorSub: "user-1",
      startedAt: 1_000, endedAt: 2_000,
      externalRefs: [`artifacts/external/${H}.mp4`, `artifacts/external/${"b".repeat(24)}.png`],
    });
  });

  it("forwards the lineage-edge fields bounded, without query strings, and keeps the message under SQS's limit", () => {
    const msg = buildTurnMessage({
      ...base,
      summary: {
        lineage: [{
          output: "s3://b/artifacts/external/x.png", inputs: ["s3://b/f.jpg"], tool: "usr-fal___run_model",
          unresolved: [{ ref: "https://h.example/a.png?X-Amz-Signature=secret", reason: "external_url" }, { nope: 1 }],
          origin: "https://v3.fal.media/files/x.png?token=t", via: "task_agent", jobId: "req-1", inferred: true,
          ranges: [{ input: "s3://b/f.jpg", start: 1.5, end: 3, unit: "s" }, { input: "s3://b/f.jpg", start: "x", end: 1 }],
          settings: { model: "flux", seed: 3 }, junk: "dropped",
        }, { output: "s3://b/o.mp4", inputs: [], settings: { prompt: "p".repeat(2000) } }],
      },
    });
    expect(msg?.lineage).toEqual([
      {
        output: "s3://b/artifacts/external/x.png", inputs: ["s3://b/f.jpg"], tool: "usr-fal___run_model",
        unresolved: [{ ref: "https://h.example/a.png", reason: "external_url" }],
        origin: "https://v3.fal.media/files/x.png", via: "task_agent", jobId: "req-1", inferred: true,
        ranges: [{ input: "s3://b/f.jpg", start: 1.5, end: 3, unit: "s" }], settings: { model: "flux", seed: 3 },
      },
      { output: "s3://b/o.mp4", inputs: [] },
    ]);
    const many = Array.from({ length: 100 }, (_, n) => ({
      output: `s3://b/artifacts/s/output/${n}.mp4`,
      inputs: Array.from({ length: 500 }, (_, i) => `s3://bucket-name/artifacts/s/output/clip_${n}_${i}.mp4`),
    }));
    const big = buildTurnMessage({ ...base, summary: { lineage: many } });
    expect(big?.lineage).toHaveLength(100);
    expect(JSON.stringify(big).length).toBeLessThan(200_000);
  });

  it("forwards the summary's lineage, dropping malformed entries and capping inputs at 500 (lineage edges)", () => {
    const inputs = Array.from({ length: 501 }, (_, i) => `s3://b/projects/p/video:${i}/a.mp4`);
    const msg = buildTurnMessage({
      ...base,
      summary: {
        lineage: [
          { output: "s3://b/artifacts/s/output/clip.mp4", inputs: ["s3://b/x.mp4", "s3://b/x.mp4", 7], tool: "crop_video" },
          { output: "", inputs: [] },
          { inputs: ["s3://b/y.mp4"] },
          "junk",
          { output: "s3://b/artifacts/s/output/frames.zip", inputs },
        ],
      },
    });
    expect(msg?.lineage).toEqual([
      { output: "s3://b/artifacts/s/output/clip.mp4", inputs: ["s3://b/x.mp4"], tool: "crop_video" },
      { output: "s3://b/artifacts/s/output/frames.zip", inputs: inputs.slice(0, 500) },
    ]);
  });

  it("omits lineage when the summary has none", () => {
    expect(buildTurnMessage({ ...base, summary: { lineage: [] } })).not.toHaveProperty("lineage");
    expect(buildTurnMessage(base)).not.toHaveProperty("lineage");
  });

  it("is null without a project, actor, session, or turn", () => {
    for (const k of ["projectId", "actorSub", "sessionId", "turnId"] as const) {
      expect(buildTurnMessage({ ...base, [k]: undefined })).toBeNull();
    }
    expect(buildTurnMessage({ ...base, endedAt: 500 })).toBeNull();
  });
});

describe("makeGeneratedMediaNotifier", () => {
  it("sends one JSON body and swallows a send failure", async () => {
    const send = vi.fn().mockRejectedValue(new Error("sqs down"));
    const err = vi.spyOn(console, "error").mockImplementation(() => {});
    makeGeneratedMediaNotifier(send)(base);
    expect(send).toHaveBeenCalledTimes(1);
    expect(JSON.parse(send.mock.calls[0][0]).kind).toBe("turn");
    await new Promise((r) => setTimeout(r, 0));
    expect(err).toHaveBeenCalled();
    err.mockRestore();
  });

  it("sends nothing for a non-Projects turn", () => {
    const send = vi.fn().mockResolvedValue({});
    makeGeneratedMediaNotifier(send)({ ...base, projectId: undefined });
    expect(send).not.toHaveBeenCalled();
  });
});
