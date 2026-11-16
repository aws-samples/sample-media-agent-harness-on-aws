import { describe, it, expect, vi } from "vitest";

// Isolate the GROUPING decision from composeTimeline's internals (which has its own tests):
// return a sentinel so tests assert WHICH manifests were composited, not HOW.
vi.mock("../src/shared/composeTimeline", () => ({
  composeTimeline: (manifests: unknown[]) => ({ template: "composite-timeline", memberCount: manifests.length }),
}));

import {
  renderTabLabel,
  deriveRenderSession,
  sessionFromArtifactUrl,
  composeRenderTabs,
  isTimelineComposable,
  isGeneratedImageGallery,
} from "./renderSession";

const SID = "51d063a9-7193-41e5-8c4a-d3e0bb72c717"; // a real 36-char session uuid

describe("sessionFromArtifactUrl", () => {
  it("extracts the session uuid from an artifacts/<uuid>/ path", () => {
    expect(sessionFromArtifactUrl(`https://cdn/artifacts/${SID}/input/x-meridian.mp4`)).toBe(SID);
    expect(sessionFromArtifactUrl(`https://cdn/artifacts/${SID}/output/aaaaaaaa-1111-2222-3333-444444444444/clip.mp4`)).toBe(SID);
  });
  it("does NOT match non-uuid segments (external, render-<ts>)", () => {
    expect(sessionFromArtifactUrl("https://cdn/artifacts/external/790444d2.jpg")).toBeNull();
    expect(sessionFromArtifactUrl("https://cdn/artifacts/render-1787851114826-pmdrgo7mvih0/render/x.json")).toBeNull();
  });
  it("extracts the session from the project layout's projects/<pid>/sessions/<uuid>/ root (Phase 3 (a))", () => {
    const PID = "p" + "a".repeat(32);
    expect(sessionFromArtifactUrl(`https://cdn/projects/${PID}/sessions/${SID}/output/clip.mp4`)).toBe(SID);
    expect(sessionFromArtifactUrl(`/api/artifact?key=projects%2F${PID}%2Fsessions%2F${SID}%2Foutput%2Fclip.mp4`)).toBe(SID);
    expect(deriveRenderSession(undefined, `https://cdn/projects/${PID}/sessions/${SID}/output/clip.mp4`)).toBe(SID);
    // A project ASSET key is not a session, and a malformed project id is not the layout.
    expect(sessionFromArtifactUrl(`https://cdn/projects/${PID}/${SID}/clip.mp4`)).toBeNull();
    expect(sessionFromArtifactUrl(`https://cdn/projects/personal/sessions/${SID}/output/clip.mp4`)).toBeNull();
  });
  it("returns null for a fal / non-artifact url", () => {
    expect(sessionFromArtifactUrl("https://v3b.fal.media/files/b/0aa/video.mp4")).toBeNull();
    expect(sessionFromArtifactUrl(undefined)).toBeNull();
  });
});

describe("deriveRenderSession", () => {
  it("prefers a valid caller session_id", () => {
    expect(deriveRenderSession(SID, "https://cdn/artifacts/other/input/x.mp4")).toBe(SID);
  });
  it("51d063a9 FIX: derives the session from the artifact url when the client sends none", () => {
    // The browser fires the source render on upload before activeSession is set, so session_id is
    // empty; without the URL fallback the manifest was orphaned and the audio never merged.
    expect(deriveRenderSession(undefined, `https://cdn/artifacts/${SID}/input/1787851099865-Netflix.mp4`)).toBe(SID);
    expect(deriveRenderSession("", `https://cdn/artifacts/${SID}/input/x.mp4`)).toBe(SID);
    expect(deriveRenderSession("short", `https://cdn/artifacts/${SID}/input/x.mp4`)).toBe(SID);
  });
  it("falls back to a random render-<ts> session only when there is no session anywhere", () => {
    const s = deriveRenderSession(undefined, "https://v3b.fal.media/files/b/0aa/video.mp4");
    expect(s.startsWith("render-")).toBe(true);
    expect(s.length).toBeGreaterThanOrEqual(33);
  });
});


describe("isTimelineComposable", () => {
  const withKey = (template: string) => ({ template, sourceKey: "src/x.png" });
  it("timeline templates WITH a sourceKey are composable", () => {
    expect(isTimelineComposable({ template: "nle-timeline", sourceKey: "v.mp4" })).toBe(true);
    expect(isTimelineComposable({ template: "composite-timeline", sourceKey: "v.mp4" })).toBe(true);
    expect(isTimelineComposable({ template: "video-annotations", sourceKey: "v.mp4" })).toBe(true);
  });
  it("non-timeline templates are NEVER composable, even with a sourceKey (session 7cf83ef3)", () => {
    expect(isTimelineComposable(withKey("image-annotated"))).toBe(false);
    expect(isTimelineComposable(withKey("data-table"))).toBe(false);
    expect(isTimelineComposable(withKey("document-viewer"))).toBe(false);
    expect(isTimelineComposable(withKey("generic-preview"))).toBe(false);
  });
  it("a manifest without a sourceKey is never composable", () => {
    expect(isTimelineComposable({ template: "nle-timeline" })).toBe(false);
    expect(isTimelineComposable({})).toBe(false);
    expect(isTimelineComposable(null)).toBe(false);
  });
});

describe("composeRenderTabs", () => {
  const item = (key: string, manifest: unknown, createdAt = "2026-01-01T00:00:00Z", label?: string) => ({
    key,
    createdAt,
    label,
    manifest,
  });

  it("keeps two image-annotated galleries that SHARE a sourceKey as two solo tabs (session 7cf83ef3)", () => {
    const sk = "files/b/same-source.png";
    const tabs = composeRenderTabs([
      item("a.json", { template: "image-annotated", sourceKey: sk, images: [{ src: "1.png" }] }, "2026-01-01T00:00:01Z"),
      item("b.json", { template: "image-annotated", sourceKey: sk, images: [{ src: "2.png" }] }, "2026-01-01T00:00:02Z"),
    ]);
    expect(tabs).toHaveLength(2); // NOT collapsed into one empty composite timeline
    expect(tabs.some((t) => t.composite === true)).toBe(false);
    expect(tabs.map((t) => t.key).sort()).toEqual(["a.json", "b.json"]);
  });

  it("composites two TIMELINE manifests that share a sourceKey into one tab", () => {
    const sk = "session/video.mp4";
    const tabs = composeRenderTabs([
      item("v.json", { template: "nle-timeline", sourceKey: sk, tracks: [{ type: "video" }] }, "2026-01-01T00:00:01Z", "Video"),
      item("a.json", { template: "nle-timeline", sourceKey: sk, tracks: [{ type: "audio" }] }, "2026-01-01T00:00:02Z", "Audio"),
    ]);
    expect(tabs).toHaveLength(1);
    expect(tabs[0].composite).toBe(true);
    expect(tabs[0].key).toBe(`composite:${sk}`);
    expect(tabs[0].memberKeys).toEqual(["v.json", "a.json"]);
    expect(tabs[0].label).toBe("Video"); // prefers the video member's label
  });

  it("a single timeline render with a sourceKey stays solo", () => {
    const tabs = composeRenderTabs([
      item("v.json", { template: "nle-timeline", sourceKey: "s/v.mp4", tracks: [{ type: "video" }] }),
    ]);
    expect(tabs).toHaveLength(1);
    expect(tabs[0].composite).toBeUndefined();
    expect(tabs[0].key).toBe("v.json");
  });

  it("mixed: video+analyses composite while image galleries sharing a key stay solo", () => {
    const vid = "session/movie.mp4";
    const img = "files/b/frame.png";
    const tabs = composeRenderTabs([
      item("vid_v.json", { template: "nle-timeline", sourceKey: vid, tracks: [{ type: "video" }] }, "2026-01-01T00:00:01Z", "Video"),
      item("vid_a.json", { template: "nle-timeline", sourceKey: vid, tracks: [{ type: "audio" }] }, "2026-01-01T00:00:02Z"),
      item("g1.json", { template: "image-annotated", sourceKey: img, images: [{ src: "1.png" }] }, "2026-01-01T00:00:03Z"),
      item("g2.json", { template: "image-annotated", sourceKey: img, images: [{ src: "2.png" }] }, "2026-01-01T00:00:04Z"),
    ]);
    const composites = tabs.filter((t) => t.composite === true);
    expect(composites).toHaveLength(1);
    expect(composites[0].memberKeys).toEqual(["vid_v.json", "vid_a.json"]);
    expect(tabs.filter((t) => t.key === "g1.json" || t.key === "g2.json")).toHaveLength(2);
    expect(tabs).toHaveLength(3);
  });
});


describe("isGeneratedImageGallery", () => {
  const gen = (extra: Record<string, unknown> = {}) => ({
    template: "image-annotated",
    renderSkill: "render-image",
    annotations: [],
    images: [{ src: "a.jpg", label: "a", annotations: [] }],
    ...extra,
  });
  it("true for a pure render-image gallery with no annotations (verified 5ef5c936 shape)", () => {
    expect(isGeneratedImageGallery(gen())).toBe(true);
    // real manifests also carry unique self sourceKeys / empty tracks/clips — irrelevant to the predicate
    expect(isGeneratedImageGallery(gen({ sourceKey: "artifacts/external/x.jpg", tracks: [], clips: [] }))).toBe(true);
  });
  it("false when the render skill is not render-image (analysis renderers)", () => {
    expect(isGeneratedImageGallery(gen({ renderSkill: "render-face" }))).toBe(false);
    expect(isGeneratedImageGallery(gen({ renderSkill: undefined }))).toBe(false);
  });
  it("false when analysis boxes are present (top-level or per-image) — analysis frame gallery", () => {
    expect(isGeneratedImageGallery(gen({ annotations: [{ x: 0, y: 0, w: 1, h: 1, label: "face" }] }))).toBe(false);
    expect(isGeneratedImageGallery(gen({ images: [{ src: "a.jpg", annotations: [{ x: 0, y: 0, w: 1, h: 1, label: "f" }] }] }))).toBe(false);
  });
  it("false for non-image templates", () => {
    expect(isGeneratedImageGallery({ template: "document-viewer", renderSkill: "render-image" })).toBe(false);
    expect(isGeneratedImageGallery({ template: "nle-timeline", renderSkill: "render-image" })).toBe(false);
    expect(isGeneratedImageGallery(null)).toBe(false);
  });
});

describe("composeRenderTabs — generated-image gallery collapse (the 16-tab fan-out fix)", () => {
  const item = (key: string, manifest: unknown, createdAt = "2026-01-01T00:00:00Z", label?: string) => ({ key, createdAt, label, manifest });
  const gen = (srcs: string[], renderGroup?: string) => ({
    template: "image-annotated",
    renderSkill: "render-image",
    annotations: [],
    // each generated render has a UNIQUE self sourceKey — the reason they fan out today
    sourceKey: `artifacts/external/${srcs[0]}`,
    ...(renderGroup ? { renderGroup } : {}),
    images: srcs.map((s) => ({ src: `https://cdn/artifacts/external/${s}`, label: s, annotations: [] })),
  });

  it("collapses N (>=2) generated-image manifests into ONE gallery; the members are absent", () => {
    const tabs = composeRenderTabs([
      item("g0.json", gen(["a.jpg", "b.jpg"]), "2026-01-01T00:00:03Z"),
      item("g1.json", gen(["c.jpg"]), "2026-01-01T00:00:04Z"),
      item("g2.json", gen(["d.jpg"]), "2026-01-01T00:00:05Z"),
    ]);
    expect(tabs).toHaveLength(1);
    const g = tabs[0];
    expect(g.key).toBe("composite:generated-images");
    expect(g.composite).toBe(true);
    expect(g.label).toBe("Generated Images");
    expect(g.memberKeys).toEqual(["g0.json", "g1.json", "g2.json"]);
    const imgs = (g.manifest as { images: { src: string }[] }).images;
    expect(imgs.map((i) => i.src)).toEqual([
      "https://cdn/artifacts/external/a.jpg",
      "https://cdn/artifacts/external/b.jpg",
      "https://cdn/artifacts/external/c.jpg",
      "https://cdn/artifacts/external/d.jpg",
    ]);
    expect((g.manifest as { template: string }).template).toBe("image-annotated");
    // no member solo tabs leaked through
    expect(tabs.some((t) => String(t.key).endsWith(".json"))).toBe(false);
  });

  it("dedupes repeated image srcs across members, preserving first-seen (generation) order", () => {
    const tabs = composeRenderTabs([
      item("g0.json", gen(["a.jpg", "b.jpg"]), "2026-01-01T00:00:01Z"),
      item("g1.json", gen(["b.jpg", "c.jpg"]), "2026-01-01T00:00:02Z"), // b repeats
    ]);
    const imgs = (tabs[0].manifest as { images: { src: string }[] }).images;
    expect(imgs.map((i) => i.src)).toEqual([
      "https://cdn/artifacts/external/a.jpg",
      "https://cdn/artifacts/external/b.jpg",
      "https://cdn/artifacts/external/c.jpg",
    ]);
  });

  it("takes the EARLIEST member timestamp so the gallery tab position is stable as images land", () => {
    const tabs = composeRenderTabs([
      item("late.json", gen(["z.jpg"]), "2026-01-01T00:09:00Z"),
      item("early.json", gen(["a.jpg"]), "2026-01-01T00:01:00Z"),
    ]);
    expect(tabs[0].createdAt).toBe("2026-01-01T00:01:00Z");
  });

  it("a SINGLE generated image stays a solo tab (no spurious gallery of one)", () => {
    const tabs = composeRenderTabs([item("only.json", gen(["a.jpg"]))]);
    expect(tabs).toHaveLength(1);
    expect(tabs[0].key).toBe("only.json");
    expect(tabs[0].composite).toBeUndefined();
  });

  it("does NOT sweep in an analysis frame gallery (boxes present) — it stays its own tab", () => {
    const tabs = composeRenderTabs([
      item("g0.json", gen(["a.jpg"]), "2026-01-01T00:00:01Z"),
      item("g1.json", gen(["b.jpg"]), "2026-01-01T00:00:02Z"),
      item("faces.json", {
        template: "image-annotated",
        renderSkill: "render-face",
        sourceKey: "session/frame.png",
        images: [{ src: "frame.png", annotations: [{ x: 1, y: 1, w: 2, h: 2, label: "Alice" }] }],
        annotations: [{ x: 1, y: 1, w: 2, h: 2, label: "Alice" }],
      }, "2026-01-01T00:00:03Z"),
    ]);
    expect(tabs).toHaveLength(2); // one gallery + the untouched faces tab
    expect(tabs.some((t) => String(t.key).startsWith("composite:generated-images"))).toBe(true);
    expect(tabs.some((t) => t.key === "faces.json" && t.composite === undefined)).toBe(true);
  });

  it("mixed scene: timeline composite + generated gallery + a document solo → 3 tabs", () => {
    const vid = "session/movie.mp4";
    const tabs = composeRenderTabs([
      item("vid_v.json", { template: "nle-timeline", sourceKey: vid, tracks: [{ type: "video" }] }, "2026-01-01T00:00:01Z", "Video"),
      item("vid_a.json", { template: "nle-timeline", sourceKey: vid, tracks: [{ type: "audio" }] }, "2026-01-01T00:00:02Z"),
      item("g0.json", gen(["a.jpg"]), "2026-01-01T00:00:03Z"),
      item("g1.json", gen(["b.jpg"]), "2026-01-01T00:00:04Z"),
      item("doc.json", { template: "document-viewer", sourceKey: "session/report.md" }, "2026-01-01T00:00:05Z", "Report"),
    ]);
    expect(tabs).toHaveLength(3);
    expect(tabs.filter((t) => t.composite === true).map((t) => t.key).sort()).toEqual(["composite:generated-images", `composite:${vid}`]);
    expect(tabs.some((t) => t.key === "doc.json" && t.composite === undefined)).toBe(true);
  });

  it("REGRESSION GUARD: image-annotated WITHOUT renderSkill (7cf83ef3) is not treated as generation", () => {
    const sk = "files/b/same-source.png";
    const tabs = composeRenderTabs([
      item("a.json", { template: "image-annotated", sourceKey: sk, images: [{ src: "1.png" }] }, "2026-01-01T00:00:01Z"),
      item("b.json", { template: "image-annotated", sourceKey: sk, images: [{ src: "2.png" }] }, "2026-01-01T00:00:02Z"),
    ]);
    expect(tabs).toHaveLength(2);
    expect(tabs.some((t) => t.composite === true)).toBe(false);
  });
});


describe("composeRenderTabs — generated images collapse to ONE gallery per session", () => {
  const item = (key: string, manifest: unknown, createdAt = "2026-01-01T00:00:00Z", label?: string) => ({ key, createdAt, label, manifest });
  const gen = (srcs: string[], renderGroup?: string) => ({
    template: "image-annotated",
    renderSkill: "render-image",
    annotations: [],
    sourceKey: `artifacts/external/${srcs[0]}`, // unique per render — can't group by source
    ...(renderGroup ? { renderGroup } : {}),
    images: srcs.map((s) => ({ src: `https://cdn/artifacts/external/${s}`, label: s, annotations: [] })),
  });

  it("collapses images from DIFFERENT turns into ONE gallery (a storyboard spans turns)", () => {
    const tabs = composeRenderTabs([
      item("a0.json", gen(["a.jpg"], "turn-A"), "2026-01-01T00:00:01Z"),
      item("a1.json", gen(["b.jpg"], "turn-A"), "2026-01-01T00:00:02Z"),
      item("b0.json", gen(["c.jpg"], "turn-B"), "2026-01-01T00:05:01Z"),
      item("b1.json", gen(["d.jpg"], "turn-B"), "2026-01-01T00:05:02Z"),
    ]);
    expect(tabs).toHaveLength(1);
    expect(tabs[0].key).toBe("composite:generated-images");
    expect(tabs[0].memberKeys).toEqual(["a0.json", "a1.json", "b0.json", "b1.json"]);
    expect((tabs[0].manifest as { images: unknown[] }).images).toHaveLength(4);
  });

  it("collapses even when each render carries a UNIQUE per-generation renderGroup (the 66bc0733 case)", () => {
    // The already-deployed build stamped a per-generation fal job id as renderGroup; that must NOT
    // fragment the gallery. Grouping is purely per-session and ignores renderGroup entirely, so an
    // already-stamped session still collapses on the next list.
    const tabs = composeRenderTabs([
      item("p0.json", gen(["p0.jpg"], "01a069ad-0e83-7093"), "2026-01-01T00:00:01Z"),
      item("p1.json", gen(["p1.jpg"], "01a069af-4f85-72d1"), "2026-01-01T00:00:02Z"),
      item("p2.json", gen(["p2.jpg"], "turn-ae91df43"), "2026-01-01T00:00:03Z"),
    ]);
    expect(tabs).toHaveLength(1);
    expect(tabs[0].key).toBe("composite:generated-images");
    expect((tabs[0].manifest as { images: unknown[] }).images).toHaveLength(3);
  });

  it("a session with only ONE generated image leaves it as a solo tab (no gallery of one)", () => {
    const tabs = composeRenderTabs([item("only.json", gen(["x.jpg"], "turn-x"))]);
    expect(tabs).toHaveLength(1);
    expect(tabs[0].key).toBe("only.json");
    expect(tabs[0].composite).toBeUndefined();
  });
});

describe('renderViewManifestError (POST /sessions/:id/renders)', () => {
  it('accepts a result gallery as a view, so a "3 frames" tab survives a refresh', async () => {
    const { renderViewManifestError } = await import('./renderSession');
    expect(renderViewManifestError({ id: 'g1', template: 'clip-gallery', galleryClips: [] })).toBeNull();
    expect(renderViewManifestError({ id: 'g2', template: 'image-annotated', images: [] })).toBeNull();
    expect(renderViewManifestError({ id: 'v', template: 'nle-timeline' })).toBeNull();
    expect(renderViewManifestError({ id: 'x', template: 'graph-view' })).toMatch(/must be one of/);
    expect(renderViewManifestError(null)).toMatch(/object/);
  });
});

describe("renderTabLabel", () => {
  const loc = "s3://b/projects/p/sessions/s/output/Netflix_Open_Content_Meridian_clip_0029-0318_dd37230.mp4";
  it("a plain media render is named after its file, not the kind ('Video', 8e2acba8)", () => {
    expect(renderTabLabel({ renderSkill: "render-video", tracks: [{ type: "video" }], sourceLocation: loc }))
      .toBe("Netflix_Open_Content_Meridian_clip_0029-0318_dd37230.mp4");
  });
  it("an analysis is named after the work; a self-named manifest keeps its title", () => {
    expect(renderTabLabel({ renderSkill: "render-shot-segmentation", tracks: [{ type: "video" }], sourceLocation: loc }))
      .toBe("Shot Segmentation");
    expect(renderTabLabel({ template: "asset-player", title: "Clip A.mp4" })).toBe("Clip A.mp4");
    expect(renderTabLabel({ renderSkill: "render-video", tracks: [{ type: "video" }] })).toBe("Video");
  });
});
