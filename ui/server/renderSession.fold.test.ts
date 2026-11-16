// R7.1: composeRenderTabs folds timeline manifests onto their source by `sourceAssetId`, else
// `sourceLocation`, else the exact `sourceKey` (legacy). An analysis whose sourceKey differs from the
// source render's (a mirror / presigned / CloudFront form of the same object) still folds when both
// name the same asset or location.
import { describe, it, expect } from "vitest";
import { composeRenderTabs, foldKeys, isTimelineComposable } from "./renderSession";

const item = (key: string, manifest: Record<string, unknown>, createdAt: string, label?: string) => ({
  key, createdAt, label, manifest: { template: "nle-timeline", tracks: [], clips: [], ...manifest },
});
const video = [{ id: "v", type: "video", src: "https://cdn/v.mp4" }];
const audio = [{ id: "a", type: "audio", src: "https://cdn/a.wav" }];

describe("composeRenderTabs — fold by source asset / location / key", () => {
  it("folds by sourceAssetId even when the sourceKeys differ", () => {
    const tabs = composeRenderTabs([
      item("src.json", { sourceAssetId: "video:1", sourceKey: "projects/p/1/nba.mp4", tracks: video }, "2026-01-01T00:00:01Z", "Video"),
      item("shots.json", { sourceAssetId: "video:1", sourceKey: "artifacts/durable/x/nba.mp4", markers: [{ id: "m1", time: 1 }] }, "2026-01-01T00:00:02Z"),
    ]);
    expect(tabs).toHaveLength(1);
    expect(tabs[0].composite).toBe(true);
    expect(tabs[0].key).toBe("composite:asset:video:1");
    expect(tabs[0].memberKeys).toEqual(["src.json", "shots.json"]);
    expect((tabs[0].manifest as { sourceAssetId?: string }).sourceAssetId).toBe("video:1");
  });

  it("falls back to sourceLocation when no asset id is known", () => {
    const loc = "s3://ingest/artifacts/s1/output/clip.mp4";
    const tabs = composeRenderTabs([
      item("src.json", { sourceLocation: loc, sourceKey: "a", tracks: video }, "2026-01-01T00:00:01Z"),
      item("stems.json", { sourceLocation: loc, sourceKey: "b", tracks: audio }, "2026-01-01T00:00:02Z"),
    ]);
    expect(tabs).toHaveLength(1);
    expect(tabs[0].key).toBe(`composite:loc:${loc}`);
  });

  it("a location resolves to the asset another manifest pairs it with", () => {
    const loc = "s3://ingest/projects/p/1/nba.mp4";
    const tabs = composeRenderTabs([
      item("src.json", { sourceAssetId: "video:1", sourceLocation: loc, tracks: video }, "2026-01-01T00:00:01Z"),
      item("faces.json", { sourceLocation: loc, annotations: [{ id: "f1" }] }, "2026-01-01T00:00:02Z"),
    ]);
    expect(tabs).toHaveLength(1);
    expect(tabs[0].key).toBe("composite:asset:video:1");
  });

  it("a legacy sourceKey-only analysis still folds onto a source that carries the same sourceKey + asset id", () => {
    const tabs = composeRenderTabs([
      item("src.json", { sourceAssetId: "video:1", sourceKey: "s/v.mp4", tracks: video }, "2026-01-01T00:00:01Z"),
      item("legacy.json", { sourceKey: "s/v.mp4", tracks: audio }, "2026-01-01T00:00:02Z"),
    ]);
    expect(tabs).toHaveLength(1);
    expect(tabs[0].memberKeys).toEqual(["src.json", "legacy.json"]);
  });

  it("legacy sourceKey-only manifests keep the historical composite key", () => {
    const tabs = composeRenderTabs([
      item("v.json", { sourceKey: "s/v.mp4", tracks: video }, "2026-01-01T00:00:01Z"),
      item("a.json", { sourceKey: "s/v.mp4", tracks: audio }, "2026-01-01T00:00:02Z"),
    ]);
    expect(tabs.map((t) => t.key)).toEqual(["composite:s/v.mp4"]);
    expect(tabs[0].sourceKey).toBe("s/v.mp4");
  });

  it("different assets never fold, even with the same sourceKey", () => {
    expect(foldKeys([
      { sourceAssetId: "video:1", sourceKey: "same" },
      { sourceAssetId: "video:2", sourceKey: "same" },
    ])).toEqual(["asset:video:1", "asset:video:2"]);
  });

  it("an asset-player view is never folded into a composite", () => {
    expect(isTimelineComposable({ template: "asset-player", sourceAssetId: "video:1" })).toBe(false);
    expect(isTimelineComposable({ template: "nle-timeline", sourceLocation: "s3://b/k" })).toBe(true);
  });
});

// Session 981a44ef: the clip a media-processing job produced is kept by the harness under the session's output
// root, while its shot segmentation names the copy M2C wrote in its proxy bucket. Same job output, two
// locations -> the shot segmentation opened its own tab instead of folding onto the clip.
describe("composeRenderTabs — two locations of one job output (session 981a44ef)", () => {
  const SK = "6e63b326-9b98-5c28-a642-6ce34791ca08/meridian_clip.mp4";
  const SESSION_COPY = "s3://skills/projects/p933/sessions/981a44ef/output/6e63b326-9b98-5c28-a642-6ce34791ca08/meridian_clip.mp4";
  const M2C_COPY = "s3://m2cv6-proxy/micro-workflow/media-processing/6e63b326-9b98-5c28-a642-6ce34791ca08/clip/meridian_clip.mp4";

  it("folds the analysis onto the clip it analysed", () => {
    const tabs = composeRenderTabs([
      item("clip.json", { sourceLocation: SESSION_COPY, sourceKey: SK, tracks: video }, "2026-10-11T12:43:30Z", "Video"),
      item("shots.json", { sourceLocation: M2C_COPY, sourceKey: SK, markers: [{ id: "m1", time: 1 }] }, "2026-10-11T12:50:00Z", "Shot Segmentation"),
      item("diarize.json", { sourceLocation: M2C_COPY, sourceKey: SK, tracks: audio }, "2026-10-11T12:55:00Z"),
    ]);
    expect(tabs).toHaveLength(1);
    expect(tabs[0].key).toBe(`composite:loc:${SESSION_COPY}`);
    expect(tabs[0].memberKeys).toEqual(["clip.json", "shots.json", "diarize.json"]);
  });

  it("never merges by a bare filename key", () => {
    expect(foldKeys([
      { sourceLocation: "s3://a/x/clip.mp4", sourceKey: "clip.mp4" },
      { sourceLocation: "s3://b/y/clip.mp4", sourceKey: "clip.mp4" },
    ])).toEqual(["loc:s3://a/x/clip.mp4", "loc:s3://b/y/clip.mp4"]);
  });

  it("never re-points a distinct asset", () => {
    expect(foldKeys([
      { sourceAssetId: "video:1", sourceKey: SK },
      { sourceAssetId: "video:2", sourceKey: SK },
    ])).toEqual(["asset:video:1", "asset:video:2"]);
  });
});

describe("composeRenderTabs — every tab says what it is about (subjectIds), so the UI groups views by subject", () => {
  // IND403 dry run: Clip opened the clip as a player tab; its shot segmentation then opened a SECOND tab on the
  // same video. The server keeps both views and names the file each one shows; the panel shows them as ONE tab
  // with two versions (renderSubject.groupBySubject).
  const CLIP = "s3://ingest/projects/p1/sessions/s1/output/0-demo_clip_0022-0237_x.mp4";
  const view = item("artifacts/s1/render/view-1.json",
    { id: "view-1", template: "asset-player", ref: { project_id: "p1", asset_id: "video:c1" }, sourceLocation: CLIP },
    "2026-11-14T19:32:44.000Z", "0-demo_clip_0022-0237_x.mp4");
  const shots = item("artifacts/s1/render/arn_shots.json",
    { id: "arn_shots", template: "nle-timeline", sourceLocation: CLIP, sourceKey: "s1/0-demo_clip_0022-0237_x.mp4",
      tracks: [{ id: "v", type: "video", src: "https://x/clip.mp4" }], markers: [{ id: "m1", time: 1 }] },
    "2026-11-14T19:35:24.000Z", "Shot Segmentation");
  const speakers = item("artifacts/s1/render/arn_spk.json",
    { id: "arn_spk", template: "nle-timeline", sourceLocation: CLIP, tracks: [{ id: "v", type: "video", src: "https://x/clip.mp4" }], segments: [{ id: "s-0", start: 0, end: 1 }] },
    "2026-11-14T19:36:00.000Z", "Speakers");

  it("a player view and a timeline of one file: both views stay, and both name the file", () => {
    const tabs = composeRenderTabs([view, shots]) as any[];
    expect(tabs.map((t) => t.key)).toEqual(["artifacts/s1/render/view-1.json", "artifacts/s1/render/arn_shots.json"]);
    expect(tabs[0].subjectIds).toEqual(["asset:video:c1", `loc:${CLIP}`]);
    expect(tabs[1].subjectIds).toContain(`loc:${CLIP}`);
  });
  it("two analyses of one file still compose into one timeline (lanes, not versions), which names the file", () => {
    const tabs = composeRenderTabs([view, shots, speakers]) as any[];
    expect(tabs).toHaveLength(2);
    const timeline = tabs.find((t) => t.composite);
    expect(timeline.memberKeys).toEqual(["artifacts/s1/render/arn_shots.json", "artifacts/s1/render/arn_spk.json"]);
    expect(timeline.subjectIds).toContain(`loc:${CLIP}`);
  });
  it("a gallery that shows no single file is about its title; a bare file name is never an identity", () => {
    const gallery = item("artifacts/s1/render/gallery-1.json", { id: "gallery-1", template: "clip-gallery", title: "Voice audition", galleryClips: [{ id: "c1", src: "https://x/a.mp3" }] },
      "2026-11-14T19:40:00.000Z", "Voice audition");
    const bare = item("artifacts/s1/render/arn_b.json", { id: "arn_b", template: "data-table", sourceKey: "0-demo.mp4", rows: [{}] },
      "2026-11-14T19:41:00.000Z", "Table");
    const tabs = composeRenderTabs([gallery, bare]) as any[];
    expect(tabs[0].subjectIds).toEqual(["title:Voice audition"]);
    expect(tabs[1].subjectIds).toEqual([]);
  });
});
