/**
 * Decide the render-agent session id for POST /render.
 *
 * The manifest the Render Agent writes lands under `artifacts/<session>/render/…`, and
 * GET /sessions/:id/renders only lists `artifacts/<sid>/render/`. So the render MUST be scoped
 * to the artifact's OWN session, else its manifest is orphaned and the UI can never list/merge it
 * (session 51d063a9: the uploaded source's render kept landing in a random `render-<ts>` dir, so
 * an analysis of the original had no source manifest to merge with → a stray solo tab).
 *
 * Precedence:
 *   1. an explicit, runtime-valid (>=33 char) `session_id` from the caller;
 *   2. else the session UUID parsed from the artifact URL's `artifacts/<uuid>/…` path — this is
 *      robust to client timing (the browser fires the source render on upload, before its
 *      `activeSession` is set, so it often can't supply the id);
 *   3. else a random `render-<ts>` session (a genuinely session-less render, e.g. an external
 *      URL with no artifacts/<sid>/ path).
 *
 * Only a real UUID segment is accepted from the URL, so `artifacts/external/…` or an existing
 * `artifacts/render-<ts>/…` never gets mistaken for a session.
 */
const _UUID = "[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}";
// Both session roots (asset-lineage Phase 3 (a)): `artifacts/<sid>/…` and the project layout's
// `projects/<pid>/sessions/<sid>/…`, in a CDN path or URL-encoded in `/api/artifact?key=`.
const _SESSION_UUID_RES = [
  new RegExp(`(?:^|/|=)artifacts(?:/|%2F)(${_UUID})(?:/|%2F)`, "i"),
  new RegExp(`(?:^|/|=)projects(?:/|%2F)p[0-9a-f]{32}(?:/|%2F)sessions(?:/|%2F)(${_UUID})(?:/|%2F)`, "i"),
];

export function sessionFromArtifactUrl(url: string | undefined | null): string | null {
  if (!url) return null;
  for (const re of _SESSION_UUID_RES) {
    const m = re.exec(String(url));
    if (m) return m[1];
  }
  return null;
}

export function deriveRenderSession(
  bodySessionId: unknown,
  artifactUrl: string | undefined | null,
): string {
  if (typeof bodySessionId === "string" && bodySessionId.length >= 33) return bodySessionId;
  const fromUrl = sessionFromArtifactUrl(artifactUrl);
  if (fromUrl) return fromUrl;
  return `render-${Date.now()}-${Math.random().toString(36).slice(2)}`.padEnd(33, "0");
}

/** The session row fields the render gate reads (`undefined` when there is no row). */
export type RenderSessionRow = { actorId?: unknown; locked?: unknown; active?: unknown } | undefined;

export type RenderSessionDecision =
  | { kind: "ok"; sessionId: string }
  | { kind: "reject"; status: number; code: string; message: string };

/**
 * `deriveRenderSession` with the session-ownership rule `/api` applies (`decideApiSession`).
 *
 * The render session decides WHERE the Render Agent writes its manifest
 * (`artifacts/<session>/render/…`), and both inputs are caller-controlled — the body's
 * `session_id`, and the session a URL names — so without a check any signed-in user could write a
 * manifest into another user's session folder (and pin a render microVM to that session id).
 *
 *   - a body `session_id` owned by someone else → 403 `session_not_owned`; one that is not a chat
 *     session id at all (`lab-…`, `EXT_…`) → 400 `invalid_session_id`; a locked one → 423 — exactly
 *     the answers `/api` gives;
 *   - a session the caller OWNS (named by the body, else by the URL) → that session, as before;
 *   - anything else — a body id with no row yet, a URL naming a session that is not the caller's,
 *     no session at all — → a fresh owner-tagged `render-<tag>-…` session (artifactOwner.ts). Its
 *     manifest is readable by the caller only, and nothing is written into a folder they do not own.
 *
 * `getRow` throws on a lookup failure; the route answers 503 (fail closed, like `/api`). Without a
 * sessions store (`getRow` null — local dev) there is no ownership to check and the legacy
 * derivation applies.
 */
export async function decideRenderSession(
  bodySessionId: unknown,
  artifactUrl: string | undefined | null,
  actorId: string | undefined,
  getRow: ((sessionId: string) => Promise<RenderSessionRow>) | null,
  now: number = Date.now(),
): Promise<RenderSessionDecision> {
  if (!getRow) return { kind: "ok", sessionId: deriveRenderSession(bodySessionId, artifactUrl) };
  if (!actorId) return { kind: "reject", status: 401, code: "missing_actor", message: "No actor id resolved for request" };
  if (typeof bodySessionId === "string" && bodySessionId.length >= 33) {
    const row = await getRow(bodySessionId);
    const decision = decideApiSession(bodySessionId, actorId, row);
    if (decision.kind === "reject") return decision;
    if (decision.locked) return { kind: "reject", status: 423, code: "session_locked", message: "Session is locked" };
    if (row) return { kind: "ok", sessionId: bodySessionId };
  } else {
    const fromUrl = sessionFromArtifactUrl(artifactUrl);
    if (fromUrl) {
      const row = await getRow(fromUrl);
      if (row && row.actorId === actorId && !row.locked && row.active !== 0) return { kind: "ok", sessionId: fromUrl };
    }
  }
  return { kind: "ok", sessionId: ownerTaggedRenderSession(actorId, now) };
}


import { composeTimeline } from "../src/shared/composeTimeline";
import { viewSubjectIds } from "../src/shared/renderSubject";
import { decideApiSession } from "./runtimeBody";
import { ownerTaggedRenderSession } from "./artifactOwner";

/** One listed render manifest (as GET /sessions/:id/renders builds it). */
export interface RenderItem {
  key: string;
  manifestId?: string;
  createdAt: string;
  label?: string;
  manifest: unknown;
}

// Templates that are NOT timeline-shaped. These describe a standalone artifact (an image
// gallery, a data table, a document) rather than a source video plus its analysis lanes, so
// they must NEVER be fed to composeTimeline: doing so yields a composite-timeline with no
// media/tracks that renders "No media source available" / "No media tracks available".
// (session 7cf83ef3: two `image-annotated` frame galleries that happened to carry the same
// source-image sourceKey were composited into one empty timeline.)
const NON_TIMELINE_TEMPLATES = new Set([
  "image-annotated",
  "data-table",
  "document-viewer",
  "generic-preview",
  // A persisted Expand view (POST /sessions/:id/renders): an Asset_Reference the player presigns at
  // view time, with no media URL a composite could play — it stays its own tab.
  "asset-player",
  // The agent's browse_project tab: a project id + filter, no media of its own.
  "project-browser",
]);

const nonEmpty = (v: unknown): string => (typeof v === "string" && v ? v : "");

/**
 * A manifest composites with its siblings ONLY when it is timeline-shaped AND names its source
 * (`sourceAssetId`, `sourceLocation` or `sourceKey`). A non-timeline template stays solo even when
 * it names one, so it routes to its own renderer (ImageAnnotated, DataTable, ...) instead of an
 * empty timeline.
 */
export function isTimelineComposable(manifest: unknown): boolean {
  const m = (manifest ?? {}) as { template?: unknown; sourceKey?: unknown; sourceAssetId?: unknown; sourceLocation?: unknown };
  const named = !!(nonEmpty(m.sourceAssetId) || nonEmpty(m.sourceLocation) || nonEmpty(m.sourceKey));
  return named && !NON_TIMELINE_TEMPLATES.has(String(m.template));
}

/**
 * The fold key of each composable manifest: its source ASSET when known (R7.1), else its canonical
 * source LOCATION, else its exact `sourceKey` (legacy manifests). A weaker identity resolves to the
 * strongest one any sibling pairs it with — a legacy `sourceKey`-only analysis still folds onto a
 * source manifest that carries `sourceKey` + `sourceAssetId`, and a `sourceLocation` resolves to the
 * asset another manifest maps it to. First pairing wins (manifests arrive oldest first).
 *
 * The bare sourceKey tier keeps the historical composite key `composite:<sourceKey>`, so existing
 * cached tabs keep their identity; the new tiers are prefixed (`asset:` / `loc:`).
 */
export function foldKeys(manifests: unknown[]): string[] {
  const ids = manifests.map((raw) => {
    const m = (raw ?? {}) as { sourceKey?: unknown; sourceAssetId?: unknown; sourceLocation?: unknown };
    return { asset: nonEmpty(m.sourceAssetId), loc: nonEmpty(m.sourceLocation), sk: nonEmpty(m.sourceKey) };
  });
  const locToAsset = new Map<string, string>();
  for (const { asset, loc } of ids) if (asset && loc && !locToAsset.has(loc)) locToAsset.set(loc, asset);
  const strongest = (asset: string, loc: string): string => {
    if (asset) return `asset:${asset}`;
    const viaLoc = loc ? locToAsset.get(loc) : undefined;
    if (viaLoc) return `asset:${viaLoc}`;
    return loc ? `loc:${loc}` : "";
  };
  const skToStrong = new Map<string, string>();
  for (const { asset, loc, sk } of ids) {
    const strong = strongest(asset, loc);
    if (sk && strong && !skToStrong.has(sk)) skToStrong.set(sk, strong);
  }
  return ids.map(({ asset, loc, sk }) => {
    const strong = strongest(asset, loc);
    // Two LOCATIONS of one job output fold together. The harness keeps its own copy of an M2C output under
    // the session's output root, while an analysis of it names the copy M2C wrote in its proxy bucket — same
    // bytes, two `s3://` locations, so a clip and its shot segmentation opened as two tabs (session 981a44ef).
    // A job-scoped sourceKey (`<execution uuid>/<basename>`) names exactly one output, so a location-only
    // identity defers to the first identity that key was paired with. Assets stay authoritative (an `asset:`
    // identity is never re-pointed), and a bare-filename key never merges unrelated files.
    if (strong.startsWith("loc:") && sk && JOB_SCOPED_SOURCE_KEY.test(sk)) {
      const first = skToStrong.get(sk);
      if (first && first !== strong) return first;
    }
    return strong || skToStrong.get(sk) || sk;
  });
}

/** `<uuid>/<basename>` — the sourceKey form that names one execution's output (render_agent.derive_source_key). */
const JOB_SCOPED_SOURCE_KEY = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\/[^/]+$/i;

/**
 * A PURE generated-image gallery: an `image-annotated` manifest produced by the image-generation
 * render skill (`render-image`) that carries NO analysis annotations anywhere (neither a top-level
 * boxes array nor per-image boxes).
 *
 * This is the discriminator that lets us collapse every generated image in a session into ONE
 * gallery tab WITHOUT sweeping in analysis frame galleries. Verified against real generation
 * manifests (session 5ef5c936): each fal image-gen render is `template:"image-annotated"`,
 * `renderSkill:"render-image"`, `annotations:[]`, `tracks:[]`, `clips:[]`, and a UNIQUE self
 * sourceKey (which is exactly why 16 of them fan out into 16 solo tabs — they can't group by
 * sourceKey). Analysis frame galleries (face recognition / shot detection) are ALSO
 * `image-annotated` but carry detection boxes and/or a different renderSkill (session 7cf83ef3),
 * so requiring render-image AND zero annotations keeps them as their own tabs.
 */
export function isGeneratedImageGallery(manifest: unknown): boolean {
  const m = (manifest ?? {}) as {
    template?: unknown;
    renderSkill?: unknown;
    annotations?: unknown;
    images?: unknown;
  };
  if (m.template !== "image-annotated") return false;
  if (m.renderSkill !== "render-image") return false;
  if (Array.isArray(m.annotations) && m.annotations.length > 0) return false;
  const imgs = Array.isArray(m.images) ? (m.images as Array<{ annotations?: unknown }>) : [];
  for (const im of imgs) {
    if (im && Array.isArray(im.annotations) && im.annotations.length > 0) return false;
  }
  return true;
}


/**
 * An UNROUTABLE RAW manifest: no `template`, no `sourceKey`, and no `errors` — nothing for the
 * RendererRegistry to route to, so it renders as a broken "raw manifest" tab. This is the shape a
 * hand-built manifest takes when the Render Agent model emits the prompt schema
 * (`{id,duration,tracks,clips,captions,createdAt,errors}`) directly instead of via the deterministic
 * builder (session 368f1233). Error manifests are judged by {@link isErrorOnlyManifest}.
 */
export function isUnroutableRawManifest(manifest: unknown): boolean {
  const m = (manifest ?? {}) as { template?: unknown; sourceKey?: unknown; errors?: unknown };
  const hasTemplate = typeof m.template === "string" && !!m.template;
  const hasSourceKey = typeof m.sourceKey === "string" && !!m.sourceKey;
  const hasErrors = Array.isArray(m.errors) && m.errors.length > 0;
  return !hasTemplate && !hasSourceKey && !hasErrors;
}


/** Manifest fields that make something worth showing (mirrors render_agent `_VIEW_CONTENT_KEYS`). */
const VIEW_CONTENT_KEYS = ["template", "tracks", "clips", "images", "tables", "rows", "document", "markers",
  "segments", "annotations", "captions", "galleryClips", "galleryMoments", "nodes"] as const;

/**
 * An ERROR-ONLY manifest: a non-empty `errors` array and nothing to show. It is a failure record, not a view —
 * there is no error view, so it rendered as "No previewable artifacts" or a file link built from its
 * `sourceUrl` (session a2d759c2's "Render Output · 1 data file" tab was a VALIDATION_FAILED record). The Render
 * Agent now writes these under `render/_errors/`, which the listing skips; this drops the ones already
 * written, so existing sessions lose their junk tabs too. A real view that also carries `errors` (a manifest
 * with tracks plus a validation note) stays.
 */
export function isErrorOnlyManifest(manifest: unknown): boolean {
  const m = (manifest ?? {}) as Record<string, unknown>;
  if (!Array.isArray(m.errors) || m.errors.length === 0) return false;
  return !VIEW_CONTENT_KEYS.some((k) => {
    const v = m[k];
    return Array.isArray(v) ? v.length > 0 : typeof v === "string" ? !!v : v != null && typeof v === "object";
  });
}

/** A manifest that is not a view at all: never a tab. */
export function isNotAView(manifest: unknown): boolean {
  return isUnroutableRawManifest(manifest) || isErrorOnlyManifest(manifest);
}

/**
 * Group render manifests into the tabs the UI shows. Two collapses happen here, and NOWHERE else
 * (this is the SOLE author of tabs, so the UI never merges client-side — which caused tabs to
 * appear/disappear/reorder as renders landed incrementally):
 *
 *   1. Timeline manifests sharing a source collapse into ONE composite (video + its analysis
 *      lanes) keyed `composite:<fold key>` — by `sourceAssetId`, else `sourceLocation`, else the
 *      exact `sourceKey` (see `foldKeys`).
 *   2. PURE generated-image manifests (see `isGeneratedImageGallery`) collapse into ONE
 *      "Generated Images" gallery PER SESSION, no matter how many image-gen dispatches / sub-agents
 *      / turns produced them — every panel of a storyboard lands together and nothing is left in
 *      its own tab. Analysis frame galleries are excluded by the predicate and stay their own tabs.
 *
 * Both use the earliest member's timestamp (stable tab position as more renders land) and only
 * collapse at >=2 members, so a lone timeline / a single generated image stays a solo tab.
 */
export function composeRenderTabs<
  T extends { key: string; createdAt: string; label?: string; manifest: unknown },
>(renders: T[]): Array<Record<string, unknown>> {
  const bySource = new Map<string, T[]>();
  const gallery: T[] = []; // pure generated-image manifests → one session gallery
  const solos: T[] = [];
  const composable = renders.filter((r) => !isNotAView(r.manifest) && isTimelineComposable(r.manifest));
  const foldKeyOf = new Map<T, string>();
  foldKeys(composable.map((r) => r.manifest)).forEach((k, i) => foldKeyOf.set(composable[i], k));
  for (const r of renders) {
    // Defense-in-depth: a manifest with NO template AND NO sourceKey AND NO errors is an
    // unroutable raw shell — the RendererRegistry can't route it, so it would surface as a broken
    // "raw manifest" tab (session 368f1233: the Render Agent model hand-built a template-less
    // manifest of the SOURCE video alongside the real render). Drop it. An error-only record (a failure,
    // not a view) is dropped too — see isErrorOnlyManifest.
    if (isNotAView(r.manifest)) continue;
    if (isTimelineComposable(r.manifest)) {
      const fk = foldKeyOf.get(r) as string;
      const arr = bySource.get(fk) ?? [];
      arr.push(r);
      bySource.set(fk, arr);
    } else if (isGeneratedImageGallery(r.manifest)) {
      gallery.push(r);
    } else {
      solos.push(r);
    }
  }
  const composed: Array<Record<string, unknown>> = [];
  for (const [fk, group] of bySource) {
    if (group.length >= 2) {
      const composite = composeTimeline(group.map((g) => g.manifest as never));
      const sk = nonEmpty((composite as { sourceKey?: unknown }).sourceKey);
      const videoMember = group.find((g) =>
        ((g.manifest as { tracks?: { type?: string }[] })?.tracks ?? []).some(
          (t) => t?.type === "video",
        ),
      );
      const createdAts = group.map((g) => g.createdAt).sort();
      composed.push({
        key: `composite:${fk}`,
        manifestId: `composite:${fk}`,
        createdAt: createdAts[0] ?? new Date().toISOString(), // earliest → stable position
        label: videoMember?.label || group[0].label,
        ...(sk ? { sourceKey: sk } : {}),
        composite: true,
        memberKeys: group.map((g) => g.key), // so the UI drops the absorbed solo tabs
        // What the tab is about: every member's file identities (design A — one tab per subject; see renderSubject).
        subjectIds: [...new Set(group.flatMap((g) => viewSubjectIds(g.manifest)))],
        manifest: composite,
      });
    } else {
      // Exactly one manifest names this source, so there is nothing to compose. It passes
      // through untouched: every time-based template name routes to the ONE timeline container
      // (RendererRegistry), so a solo analysis already renders on its media's timeline without
      // this layer rewriting anything. An earlier version promoted the template here — that was
      // the symptom-level fix for staging 9e977f30, made redundant by the container fold.
      composed.push(...group.map((g) => ({ ...g, subjectIds: viewSubjectIds(g.manifest) }) as Record<string, unknown>));
    }
  }
  // Collapse #2: generated images collapse into ONE gallery PER SESSION (composeRenderTabs only
  // ever sees a single session's manifests). Users want every panel of a storyboard together —
  // across turns, dispatches, and sub-agents — with nothing left in its own tab, so we deliberately
  // do NOT sub-divide by any per-render/per-turn key. Concatenate every member's images, deduped by
  // src (a member may repeat an image, and 1-/2-image members interleave), preserving first-seen
  // (generation) order. >=2 to collapse; a lone generated image stays a solo tab.
  if (gallery.length >= 2) {
    const seen = new Set<string>();
    const images: Array<Record<string, unknown>> = [];
    for (const g of gallery) {
      const gm = g.manifest as { images?: Array<Record<string, unknown>>; src?: unknown };
      const arr = Array.isArray(gm.images)
        ? gm.images
        : typeof gm.src === "string" && gm.src
          ? [{ src: gm.src }]
          : [];
      for (const im of arr) {
        const s = String((im as { src?: unknown }).src ?? "");
        if (!s || seen.has(s)) continue;
        seen.add(s);
        images.push(im);
      }
    }
    const createdAts = gallery.map((g) => g.createdAt).sort();
    composed.push({
      key: "composite:generated-images",
      manifestId: "composite:generated-images",
      createdAt: createdAts[0] ?? new Date().toISOString(), // earliest → stable position
      label: "Generated Images",
      composite: true,
      memberKeys: gallery.map((g) => g.key), // so the UI drops the absorbed solo tabs
      manifest: {
        template: "image-annotated",
        renderSkill: "render-image",
        src: images[0] ? (images[0] as { src?: unknown }).src : undefined,
        images,
        annotations: [],
      },
    });
  } else {
    composed.push(...(gallery as unknown as Array<Record<string, unknown>>));
  }
  // Every other view carries what it is about, so the UI shows all views of one subject as ONE tab with versions
  // (a player of a file and that file's timeline, a re-run gallery) instead of one tab each.
  composed.push(...solos.map((r) => ({ ...r, subjectIds: viewSubjectIds(r.manifest) }) as Record<string, unknown>));
  composed.sort((a, b) => (String(a.createdAt) < String(b.createdAt) ? -1 : 1));
  return composed;
}


// ---- Render-store writes (POST / PATCH / DELETE /sessions/:id/renders) ----

/**
 * Templates a client may persist as a render view: an Expand asset-player, a timeline, or a result gallery
 * (`clip-gallery` / `image-annotated`, opened for a batch of direct-action outputs). The galleries were refused
 * with a 400, so a "3 frames" tab vanished on refresh (staging 28391ced).
 */
export const RENDER_VIEW_TEMPLATES = new Set(["asset-player", "nle-timeline", "composite-timeline", "clip-gallery",
  "image-annotated"]);
/** Body ceiling for a persisted view (a composite timeline manifest is the large case). */
export const RENDER_VIEW_MAX_BYTES = 256 * 1024;
/** Body ceiling for a viewState PATCH (four small fields). */
export const RENDER_VIEW_PATCH_MAX_BYTES = 4 * 1024;

const RENDER_KEY_RE = /^artifacts\/[^/]+\/render\/[\w.\-]+\.json$/i;
const SESSION_ID_RE = /^[\w-]{1,128}$/;

/** A session id that is safe to splice into an S3 key (no `/`, no dots). */
export function isSafeSessionId(sessionId: string): boolean {
  return SESSION_ID_RE.test(sessionId);
}

/** `key` is a render manifest directly under THIS session's render prefix (no traversal). */
export function isSessionRenderKey(sessionId: string, key: string): boolean {
  return key.startsWith(`artifacts/${sessionId}/render/`) && RENDER_KEY_RE.test(key) && !key.includes("..");
}

/**
 * The object name for a POSTed view: the manifest's own `id`, reduced to `[\w.-]` (so the client can
 * predict the key it will get back), else `view-<hash of the body>`.
 */
export function renderViewId(manifest: Record<string, unknown>, bodyHash: string): string {
  const raw = typeof manifest.id === "string" ? manifest.id : "";
  const clean = raw.replace(/[^\w.\-]/g, "-").replace(/\.{2,}/g, ".").replace(/^[.\-]+/, "").slice(0, 96);
  return clean || `view-${bodyHash.slice(0, 24)}`;
}

/** Why a POSTed manifest is refused, or `null` when it may be stored. */
export function renderViewManifestError(manifest: unknown): string | null {
  if (!manifest || typeof manifest !== "object" || Array.isArray(manifest)) return "manifest must be an object";
  const template = (manifest as { template?: unknown }).template;
  if (typeof template !== "string" || !RENDER_VIEW_TEMPLATES.has(template)) {
    return `manifest.template must be one of ${[...RENDER_VIEW_TEMPLATES].join(", ")}`;
  }
  return null;
}


/**
 * A restored tab's name. A manifest carries `renderSkill` but no tab label (the label only ever rode the stream), so
 * the work names it ("render-shot-segmentation" -> "Shot Segmentation"); a manifest that names itself (`title`, an
 * `asset-player` from show_asset) keeps that. The skill that merely RENDERS that kind of media ("render-video" for a
 * video track) names nothing: every plain video tab was restored as "Video" (8e2acba8), so such a tab is named
 * after its file.
 */
export function renderTabLabel(manifest: Record<string, any>): string {
  const skill = String(manifest?.renderSkill ?? "").split(",")[0].trim();
  const kinds: string[] = Array.isArray(manifest?.tracks)
    ? manifest.tracks.map((t: any) => String(t?.type ?? "").toLowerCase()).filter(Boolean) : [];
  if (skill && kinds.some((k) => skill.toLowerCase() === `render-${k}`)) {
    const location = typeof manifest.sourceLocation === "string" ? manifest.sourceLocation : "";
    const name = location.split("?")[0].split("/").pop() ?? "";
    let file = name;
    try { file = decodeURIComponent(name); } catch { /* a malformed escape keeps the raw name */ }
    if (file) return file;
  }
  const fromSkill = skill.replace(/^render[-_]/, "").split(/[-_\s]+/).filter(Boolean)
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1)).join(" ");
  return fromSkill || (typeof manifest?.title === "string" ? manifest.title.trim() : "");
}
