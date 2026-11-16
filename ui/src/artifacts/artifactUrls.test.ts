/**
 * @vitest-environment jsdom
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

const authFetch = vi.hoisted(() => vi.fn());
vi.mock('../auth/authFetch', () => ({ authFetch }));
const getConfig = vi.hoisted(() => vi.fn(async () => ({ artifactsDomain: '' })));
vi.mock('../lab-api', () => ({ getConfig }));

import {
  __resetArtifactUrlsForTests,
  artifactKeyOf,
  ensureArtifactsDomain,
  hasArtifactRefs,
  invalidateArtifactUrl,
  keyForUrl,
  peekArtifactUrl,
  preserveDocumentPreview,
  resolveArtifactUrls,
  resolveArtifactUrlsDeep,
  resolveArtifactUrlsDeepSync,
  resolveArtifactUrlsInHtml,
  htmlHasArtifactUrls,
  s3RestArtifactKeyOf,
  setArtifactsDomain,
  unresolveArtifactUrls,
} from './artifactUrls';
import { makeFakePresigner, type FakePresigner } from './testPresigner';

const CDN = 'https://d1abcd.cloudfront.net';
let presigner: FakePresigner;

beforeEach(() => {
  __resetArtifactUrlsForTests();
  presigner = makeFakePresigner();
  authFetch.mockReset();
  authFetch.mockImplementation(presigner.handle);
  getConfig.mockReset();
  getConfig.mockResolvedValue({ artifactsDomain: '' });
});
afterEach(() => {
  vi.useRealTimers();
});

describe('artifactKeyOf', () => {
  it('maps a CDN URL on *.cloudfront.net to its (decoded) key', () => {
    expect(artifactKeyOf(`${CDN}/artifacts/sess-1/clip.mp4`)).toBe('artifacts/sess-1/clip.mp4');
    expect(artifactKeyOf(`${CDN}/artifacts/a%20b/c%2Bd.png?v=1#frag`)).toBe('artifacts/a b/c+d.png');
  });

  it('maps a CDN URL on the configured custom artifacts domain (from /config)', async () => {
    expect(artifactKeyOf('https://media.example.com/artifacts/x.png')).toBeNull();
    getConfig.mockResolvedValue({ artifactsDomain: 'media.example.com' });
    await ensureArtifactsDomain();
    expect(artifactKeyOf('https://MEDIA.example.com/artifacts/x.png')).toBe('artifacts/x.png');
    // An explicit override wins without /config.
    __resetArtifactUrlsForTests();
    expect(artifactKeyOf('https://other.example.com/artifacts/y.png', 'other.example.com')).toBe('artifacts/y.png');
  });

  it('maps the /api/artifact proxy form, relative or absolute same-origin, URL-decoded, collapsing a doubled prefix', () => {
    expect(artifactKeyOf('/api/artifact?key=artifacts%2Fs%2Fdoc.pdf')).toBe('artifacts/s/doc.pdf');
    expect(artifactKeyOf('/api/artifact?key=artifacts/s/doc.pdf&download=1')).toBe('artifacts/s/doc.pdf');
    expect(artifactKeyOf(`${window.location.origin}/api/artifact?key=artifacts%2Fx.json`)).toBe('artifacts/x.json');
    expect(artifactKeyOf('/api/artifact?key=artifacts/artifacts/x.json')).toBe('artifacts/x.json');
  });

  it('maps a bare artifacts/ key', () => {
    expect(artifactKeyOf('artifacts/durable/a.mp4')).toBe('artifacts/durable/a.mp4');
    expect(artifactKeyOf('  artifacts/a.mp4 ')).toBe('artifacts/a.mp4');
  });

  it('returns null for presigned S3, fal/third-party, data:/blob:, traversal, and non-artifact keys', () => {
    expect(artifactKeyOf('https://bucket.s3.us-west-2.amazonaws.com/artifacts/x.mp4?X-Amz-Signature=abc')).toBeNull();
    expect(artifactKeyOf('https://m2c-proxy.s3.amazonaws.com/proxies/x.mp4?X-Amz-Signature=abc')).toBeNull();
    expect(artifactKeyOf('https://v3.fal.media/files/abc/out.mp4')).toBeNull();
    expect(artifactKeyOf('https://example.com/artifacts/x.png')).toBeNull();
    expect(artifactKeyOf('https://evil.example/api/artifact?key=artifacts/x.png')).toBeNull();
    expect(artifactKeyOf('data:image/png;base64,AAAA')).toBeNull();
    expect(artifactKeyOf('blob:http://localhost/1234')).toBeNull();
    expect(artifactKeyOf(`${CDN}/artifacts/../users/secret.json`)).toBeNull();
    expect(artifactKeyOf(`${CDN}/artifacts/%2E%2E/users/secret.json`)).toBeNull();
    expect(artifactKeyOf('artifacts/../users/x')).toBeNull();
    expect(artifactKeyOf('/api/artifact?key=users%2Fx.json')).toBeNull();
    expect(artifactKeyOf('/api/artifact?key=artifacts%5Cx')).toBeNull();
    expect(artifactKeyOf(`${CDN}/users/x.png`)).toBeNull();
    expect(artifactKeyOf('artifacts/')).toBeNull();
    expect(artifactKeyOf('')).toBeNull();
    expect(artifactKeyOf('relative/artifacts/x.png')).toBeNull();
  });

  it('returns null for a string that cannot name one artifact (backslash, CR/LF, second http(s)://)', () => {
    // Session 865950fd: an agent wrote two links into one previewUrl, joined by a literal `\n`.
    const joined = `${CDN}/artifacts/865950fd/output/compliance_report.json\\nhttps://d1abcd.cloudfront.net/artifacts/865950fd/output/compliance_report.pdf`;
    expect(artifactKeyOf(joined)).toBeNull();
    expect(artifactKeyOf(`${CDN}/artifacts/s/a.json\n${CDN}/artifacts/s/a.pdf`)).toBeNull();
    expect(artifactKeyOf(`${CDN}/artifacts/s/a\rb.pdf`)).toBeNull();
    expect(artifactKeyOf(`${CDN}/artifacts/s/a%0Ab.pdf`)).toBeNull();
    expect(artifactKeyOf(`${CDN}/artifacts/s/a%5Cb.pdf`)).toBeNull();
    expect(artifactKeyOf('artifacts/s\\a.pdf')).toBeNull();
    expect(artifactKeyOf('artifacts/s/a.pdf https://x.example/b.pdf')).toBeNull();
    expect(artifactKeyOf(`${CDN}/artifacts/s/a.pdfhttps://x.example/b.pdf`)).toBeNull();
    expect(artifactKeyOf('/api/artifact?key=artifacts/s/a.pdf%0A')).toBeNull();
    // Surrounding whitespace (including a trailing newline) is still trimmed, not refused.
    expect(artifactKeyOf(`${CDN}/artifacts/s/a.pdf\n`)).toBe('artifacts/s/a.pdf');
  });
});

describe('resolveArtifactUrls', () => {
  it('batches keys requested in the same tick into one POST', async () => {
    const [a, b] = await Promise.all([
      resolveArtifactUrls(['artifacts/a.png']),
      resolveArtifactUrls(['artifacts/b.png', 'artifacts/a.png']),
    ]);
    expect(authFetch).toHaveBeenCalledTimes(1);
    const [url, init] = authFetch.mock.calls[0];
    expect(url).toBe('/api/artifact-urls');
    expect(init.method).toBe('POST');
    expect(JSON.parse(init.body)).toEqual({ keys: ['artifacts/a.png', 'artifacts/b.png'] });
    expect(a.get('artifacts/a.png')?.url).toBe(presigner.urlFor('artifacts/a.png'));
    expect(b.get('artifacts/b.png')?.url).toBe(presigner.urlFor('artifacts/b.png'));
    expect(b.get('artifacts/a.png')?.url).toBe(presigner.urlFor('artifacts/a.png'));
  });

  it('chunks to 100 keys per request', async () => {
    const keys = Array.from({ length: 250 }, (_, i) => `artifacts/k${i}.png`);
    const out = await resolveArtifactUrls(keys);
    expect(presigner.calls.map((c) => c.keys.length)).toEqual([100, 100, 50]);
    expect(out.size).toBe(250);
  });

  it('de-duplicates a key already in flight', async () => {
    const p1 = resolveArtifactUrls(['artifacts/a.png']);
    await Promise.resolve(); // first request flushed and in flight
    const p2 = resolveArtifactUrls(['artifacts/a.png']);
    const [r1, r2] = await Promise.all([p1, p2]);
    expect(authFetch).toHaveBeenCalledTimes(1);
    expect(r1.get('artifacts/a.png')).toEqual(r2.get('artifacts/a.png'));
  });

  it('caches until 60 s before expiresAt, then asks again', async () => {
    vi.useFakeTimers({ toFake: ['Date'] });
    vi.setSystemTime(new Date('2026-09-20T00:00:00Z'));
    await resolveArtifactUrls(['artifacts/a.png']);
    await resolveArtifactUrls(['artifacts/a.png']);
    expect(authFetch).toHaveBeenCalledTimes(1);
    expect(peekArtifactUrl('artifacts/a.png')).not.toBeNull();

    vi.setSystemTime(new Date('2026-09-20T00:58:59Z')); // 61 s before expiry: still cached
    await resolveArtifactUrls(['artifacts/a.png']);
    expect(authFetch).toHaveBeenCalledTimes(1);

    vi.setSystemTime(new Date('2026-09-20T00:59:01Z')); // inside the 60 s skew
    expect(peekArtifactUrl('artifacts/a.png')).toBeNull();
    await resolveArtifactUrls(['artifacts/a.png']);
    expect(authFetch).toHaveBeenCalledTimes(2);
  });

  it('invalidates by key or by the resolved URL (reverse map), and keeps the old URL mappable', async () => {
    await resolveArtifactUrls(['artifacts/a.png']);
    const old = presigner.urlFor('artifacts/a.png');
    expect(keyForUrl(old)).toBe('artifacts/a.png');

    invalidateArtifactUrl(old);
    presigner.generation = 2;
    const fresh = await resolveArtifactUrls(['artifacts/a.png']);
    expect(authFetch).toHaveBeenCalledTimes(2);
    expect(fresh.get('artifacts/a.png')?.url).toBe(presigner.urlFor('artifacts/a.png', false, 2));
    expect(keyForUrl(old)).toBe('artifacts/a.png');

    invalidateArtifactUrl('artifacts/a.png');
    await resolveArtifactUrls(['artifacts/a.png']);
    expect(authFetch).toHaveBeenCalledTimes(3);

    invalidateArtifactUrl(`${CDN}/artifacts/a.png`);
    await resolveArtifactUrls(['artifacts/a.png']);
    expect(authFetch).toHaveBeenCalledTimes(4);
  });

  it('presigns downloads separately with download: true', async () => {
    await resolveArtifactUrls(['artifacts/a.png']);
    const dl = await resolveArtifactUrls(['artifacts/a.png'], { download: true });
    expect(presigner.calls).toEqual([
      { keys: ['artifacts/a.png'], download: false },
      { keys: ['artifacts/a.png'], download: true },
    ]);
    expect(JSON.parse(authFetch.mock.calls[1][1].body)).toEqual({ keys: ['artifacts/a.png'], download: true });
    expect(dl.get('artifacts/a.png')?.url).toBe(presigner.urlFor('artifacts/a.png', true));
  });

  it('asks for FRAME urls (view tickets) with frame: true, in their own cache namespace', async () => {
    await resolveArtifactUrls(['artifacts/a.pdf']);
    const fr = await resolveArtifactUrls(['artifacts/a.pdf'], { frame: true });
    expect(authFetch).toHaveBeenCalledTimes(2);
    expect(JSON.parse(authFetch.mock.calls[1][1].body)).toEqual({ keys: ['artifacts/a.pdf'], frame: true });
    expect(fr.get('artifacts/a.pdf')).toBeTruthy();
    // Cached per mode: asking again for a frame url makes no request; invalidation clears it too.
    await resolveArtifactUrls(['artifacts/a.pdf'], { frame: true });
    expect(authFetch).toHaveBeenCalledTimes(2);
    expect(peekArtifactUrl('artifacts/a.pdf', { frame: true })).not.toBeNull();
    invalidateArtifactUrl('artifacts/a.pdf');
    expect(peekArtifactUrl('artifacts/a.pdf', { frame: true })).toBeNull();
  });

  it('skips keys that fail the policy shape without a request', async () => {
    const out = await resolveArtifactUrls(['users/x.png', 'artifacts/../x', 'artifacts/a\\b']);
    expect(out.size).toBe(0);
    expect(authFetch).not.toHaveBeenCalled();
  });

  it('never throws: network error, non-200, malformed body, and per-key errors all leave keys unresolved', async () => {
    presigner.reject = true;
    await expect(resolveArtifactUrls(['artifacts/a.png'])).resolves.toEqual(new Map());

    presigner.reject = false;
    presigner.failStatus = 500;
    await expect(resolveArtifactUrls(['artifacts/a.png'])).resolves.toEqual(new Map());

    authFetch.mockResolvedValueOnce(new Response('<html>spa</html>', { status: 200 }));
    await expect(resolveArtifactUrls(['artifacts/a.png'])).resolves.toEqual(new Map());

    authFetch.mockResolvedValueOnce(new Response(JSON.stringify({ urls: { 'artifacts/a.png': { url: 42 } } }), { status: 200 }));
    await expect(resolveArtifactUrls(['artifacts/a.png'])).resolves.toEqual(new Map());

    presigner.failStatus = null;
    presigner.notFound.add('artifacts/gone.png');
    const out = await resolveArtifactUrls(['artifacts/gone.png', 'artifacts/ok.png']);
    expect([...out.keys()]).toEqual(['artifacts/ok.png']);
    // A refused key is not re-asked immediately (no request storm from re-renders).
    const before = authFetch.mock.calls.length;
    await resolveArtifactUrls(['artifacts/gone.png']);
    expect(authFetch.mock.calls.length).toBe(before);
  });
});

// A realistic NLE manifest: tracks, artifacts list, a document preview, a clip gallery, plus
// strings that must NOT be touched.
function nleManifest() {
  return {
    id: 'm-1',
    template: 'nle-timeline',
    sourceKey: 'artifacts/sess-1/source.mp4',
    duration: 42,
    tracks: [
      { id: 'v', type: 'video', src: `${CDN}/artifacts/sess-1/source.mp4`, start: 0, end: 42 },
      { id: 'a', type: 'audio', src: '/api/artifact?key=artifacts%2Fsess-1%2Fstem.wav', start: 0, end: 42 },
      { id: 'p', type: 'video', src: 'https://m2c-proxy.s3.us-west-2.amazonaws.com/proxy.mp4?X-Amz-Signature=keep', start: 0, end: 42 },
      { id: 'f', type: 'video', src: 'https://v3.fal.media/files/abc/gen.mp4', start: 0, end: 5 },
    ],
    artifacts: [
      { label: 'Frame', url: `${CDN}/artifacts/sess-1/frame.png` },
      { label: 'Report', url: 'artifacts/sess-1/report.pdf' },
    ],
    document: { previewUrl: `${CDN}/artifacts/sess-1/report.pdf`, pageCount: 3 },
    galleryClips: [
      { id: 'c1', label: 'Take 1', src: `${CDN}/artifacts/durable/take1.mp4`, kind: 'video' },
      { id: 'c2', label: 'Take 2', src: `${CDN}/artifacts/durable/take1.mp4`, kind: 'video' },
    ],
    metadata: { note: 'plain text', count: 3, ok: true },
    clips: [],
  };
}

describe('resolveArtifactUrlsDeep', () => {
  it('resolves every identifier in one batch and leaves presigned/fal URLs, keys and plain values alone', async () => {
    const input = nleManifest();
    const snapshot = JSON.parse(JSON.stringify(input));
    const out = await resolveArtifactUrlsDeep(input);

    expect(authFetch).toHaveBeenCalledTimes(1);
    expect(new Set(presigner.calls[0].keys)).toEqual(new Set([
      'artifacts/sess-1/source.mp4', 'artifacts/sess-1/stem.wav', 'artifacts/sess-1/frame.png',
      'artifacts/sess-1/report.pdf', 'artifacts/durable/take1.mp4',
    ]));

    expect(out.tracks[0].src).toBe(presigner.urlFor('artifacts/sess-1/source.mp4'));
    expect(out.tracks[1].src).toBe(presigner.urlFor('artifacts/sess-1/stem.wav'));
    expect(out.tracks[2].src).toBe(snapshot.tracks[2].src);
    expect(out.tracks[3].src).toBe('https://v3.fal.media/files/abc/gen.mp4');
    expect(out.artifacts[0].url).toBe(presigner.urlFor('artifacts/sess-1/frame.png'));
    expect(out.artifacts[1].url).toBe(presigner.urlFor('artifacts/sess-1/report.pdf'));
    expect(out.document.previewUrl).toBe(presigner.urlFor('artifacts/sess-1/report.pdf'));
    expect(out.galleryClips[0].src).toBe(presigner.urlFor('artifacts/durable/take1.mp4'));
    expect(out.galleryClips[1].src).toBe(presigner.urlFor('artifacts/durable/take1.mp4'));
    // Identity fields are not loads.
    expect(out.sourceKey).toBe('artifacts/sess-1/source.mp4');
    expect(out.metadata).toEqual(snapshot.metadata);
    // A deep COPY: the input is untouched.
    expect(input).toEqual(snapshot);
    expect(out).not.toBe(input);
  });

  it('honours a skip predicate (DocumentViewer keeps its /api/artifact preview)', async () => {
    const out = await resolveArtifactUrlsDeep(nleManifest(), { skip: preserveDocumentPreview });
    expect(out.document.previewUrl).toBe(`${CDN}/artifacts/sess-1/report.pdf`);
    // Each document of a multi-document manifest keeps its preview path too.
    const multi = await resolveArtifactUrlsDeep(
      { ...nleManifest(), documents: [{ previewUrl: `${CDN}/artifacts/sess-1/a.pdf` }, { previewUrl: `${CDN}/artifacts/sess-1/b.pdf` }] },
      { skip: preserveDocumentPreview });
    expect(multi.documents.map((d: { previewUrl: string }) => d.previewUrl))
      .toEqual([`${CDN}/artifacts/sess-1/a.pdf`, `${CDN}/artifacts/sess-1/b.pdf`]);
    expect(out.tracks[0].src).toBe(presigner.urlFor('artifacts/sess-1/source.mp4'));
  });

  it('leaves strings that fail to resolve unchanged', async () => {
    presigner.notFound.add('artifacts/sess-1/frame.png');
    const out = await resolveArtifactUrlsDeep(nleManifest());
    expect(out.artifacts[0].url).toBe(`${CDN}/artifacts/sess-1/frame.png`);
    expect(out.tracks[0].src).toBe(presigner.urlFor('artifacts/sess-1/source.mp4'));

    presigner.reject = true;
    __resetArtifactUrlsForTests();
    const untouched = await resolveArtifactUrlsDeep(nleManifest());
    expect(untouched).toEqual(nleManifest());
  });

  it('makes no request for data without identifiers', async () => {
    const data = { rows: [{ a: 1 }], url: 'https://example.com/x' };
    expect(hasArtifactRefs(data)).toBe(false);
    expect(await resolveArtifactUrlsDeep(data)).toEqual(data);
    expect(authFetch).not.toHaveBeenCalled();
  });

  it('has a sync variant for a warm cache', async () => {
    expect(resolveArtifactUrlsDeepSync(nleManifest())).toBeNull();
    const asyncOut = await resolveArtifactUrlsDeep(nleManifest());
    expect(resolveArtifactUrlsDeepSync(nleManifest())).toEqual(asyncOut);
    expect(resolveArtifactUrlsDeepSync({ n: 1 })).toEqual({ n: 1 });
  });

  it('unresolveArtifactUrls maps presigned URLs in a payload (and inside prose) back to identifiers', async () => {
    const out = await resolveArtifactUrlsDeep(nleManifest());
    const src = out.tracks[0].src;
    const event = {
      type: 'action',
      data: { verb: 'crop', src, prompt: `Crop this clip [source: ${src}] please`, other: 'https://v3.fal.media/x.mp4' },
    };
    expect(unresolveArtifactUrls(event)).toEqual({
      type: 'action',
      data: {
        verb: 'crop',
        src: `${CDN}/artifacts/sess-1/source.mp4`,
        prompt: `Crop this clip [source: ${CDN}/artifacts/sess-1/source.mp4] please`,
        other: 'https://v3.fal.media/x.mp4',
      },
    });
  });
});

describe('resolveArtifactUrlsInHtml', () => {
  it('substitutes artifact URLs in attributes and CSS url(), escape-safe, leaving text, scripts and other hosts alone', async () => {
    const html = [
      `<img src="${CDN}/artifacts/s/a.png" alt="a">`,
      `<video poster='/api/artifact?key=artifacts%2Fs%2Fposter.jpg&amp;download=1' src="https://v3.fal.media/x.mp4"></video>`,
      `<div style="background:url(&quot;${CDN}/artifacts/s/bg.jpg&quot;)">text ${CDN}/artifacts/s/a.png</div>`,
      `<style>.hero{background-image:url('${CDN}/artifacts/s/hero.jpg')}</style>`,
      `<script>const u = "${CDN}/artifacts/s/a.png";</script>`,
      `<a href="https://bucket.s3.amazonaws.com/artifacts/s/a.png?X-Amz-Signature=z">p</a>`,
    ].join('\n');
    const out = await resolveArtifactUrlsInHtml(html);
    expect(authFetch).toHaveBeenCalledTimes(1);
    expect(new Set(presigner.calls[0].keys)).toEqual(new Set([
      'artifacts/s/a.png', 'artifacts/s/poster.jpg', 'artifacts/s/bg.jpg', 'artifacts/s/hero.jpg',
    ])); // the signed S3 href names artifacts/s/a.png too (same key, one request)
    const esc = (u: string) => u.replace(/&/g, '&amp;');
    expect(out).toContain(`<img src="${esc(presigner.urlFor('artifacts/s/a.png'))}" alt="a">`);
    expect(out).toContain(`poster='${esc(presigner.urlFor('artifacts/s/poster.jpg'))}'`);
    expect(out).toContain('src="https://v3.fal.media/x.mp4"');
    expect(out).toContain(`style="background:url(&quot;${esc(presigner.urlFor('artifacts/s/bg.jpg'))}&quot;)"`);
    // Text content is not an attribute: unchanged.
    expect(out).toContain(`text ${CDN}/artifacts/s/a.png</div>`);
    // A raw-text <style> body is NOT entity-decoded, so the URL goes in raw.
    expect(out).toContain(`url('${presigner.urlFor('artifacts/s/hero.jpg')}')`);
    expect(out).toContain(`<script>const u = "${CDN}/artifacts/s/a.png";</script>`);
    // A signed S3 URL of our content baked into the page is re-signed by key (its own signature expires).
    expect(out).toContain(`href="${esc(presigner.urlFor('artifacts/s/a.png'))}"`);

    // The substituted document parses back to the presigned URL.
    const doc = new DOMParser().parseFromString(out, 'text/html');
    expect(doc.querySelector('img')!.getAttribute('src')).toBe(presigner.urlFor('artifacts/s/a.png'));
    expect(doc.querySelector('video')!.getAttribute('poster')).toBe(presigner.urlFor('artifacts/s/poster.jpg'));
  });

  // Prod session d52d544e: a sandbox-built picture-book page carried signed S3 URLs; they die after 6 h.
  it('re-signs an expired signed S3 URL baked into agent HTML, by its key', async () => {
    const baked = 'https://store.s3.amazonaws.com/artifacts/external/47a92488316043d3c789ab82.jpg'
      + '?X-Amz-Algorithm=AWS4-HMAC-SHA256&amp;X-Amz-Date=20261012T012257Z&amp;X-Amz-Expires=21600&amp;X-Amz-Signature=old';
    const html = `<img src="${baked}">`;
    expect(htmlHasArtifactUrls(html)).toBe(true);
    const out = await resolveArtifactUrlsInHtml(html);
    expect(presigner.calls[0].keys).toEqual(['artifacts/external/47a92488316043d3c789ab82.jpg']);
    const doc = new DOMParser().parseFromString(out, 'text/html');
    expect(doc.querySelector('img')!.getAttribute('src')).toBe(presigner.urlFor('artifacts/external/47a92488316043d3c789ab82.jpg'));
  });

  it('returns the HTML unchanged when nothing resolves or nothing matches', async () => {
    expect(await resolveArtifactUrlsInHtml('<p>hello</p>')).toBe('<p>hello</p>');
    expect(authFetch).not.toHaveBeenCalled();
    presigner.failStatus = 503;
    const html = `<img src="${CDN}/artifacts/s/a.png">`;
    expect(await resolveArtifactUrlsInHtml(html)).toBe(html);
  });
});

describe('domain from a mocked lab-api without getConfig', () => {
  it('does not throw when getConfig is unavailable', async () => {
    getConfig.mockRejectedValue(new Error('No "getConfig" export is defined on the mock'));
    await expect(ensureArtifactsDomain()).resolves.toBeUndefined();
    setArtifactsDomain('x.example');
    expect(artifactKeyOf('https://x.example/artifacts/a.png')).toBe('artifacts/a.png');
  });
});

// An M2C_REFERENCE (`artifacts/m2c/<bucket>/<key>`, asset-lineage D4/R4) is an artifact key like
// any other HERE — the batched resolver asks for it and the proxy presigns it in the M2C proxy
// bucket. Its one canonical form is the bare key: there is no object at the matching CDN path, so
// a CDN URL would be a link that looks fetchable and 403s.
const M2C_REF = 'artifacts/m2c/m2cv6-proxy-bucket/micro-workflow/media-processing/EXEC/proxy/transcoded.mp4';

describe('m2c references', () => {
  it('is recognised as an artifact key in every identifier form', () => {
    setArtifactsDomain('d1abcd.cloudfront.net');
    expect(artifactKeyOf(M2C_REF)).toBe(M2C_REF);
    expect(artifactKeyOf(`/api/artifact?key=${encodeURIComponent(M2C_REF)}`)).toBe(M2C_REF);
    expect(artifactKeyOf(`${CDN}/${M2C_REF}`)).toBe(M2C_REF);
  });

  it('resolves through the same batched request as any other key', async () => {
    const r = await resolveArtifactUrls([M2C_REF]);
    expect(presigner.calls).toEqual([{ keys: [M2C_REF], download: false }]);
    expect(r.get(M2C_REF)?.url).toBe(presigner.urlFor(M2C_REF));
    expect(keyForUrl(presigner.urlFor(M2C_REF))).toBe(M2C_REF);
  });

  it('unresolves back to the BARE key, never to a CDN URL that does not exist', async () => {
    setArtifactsDomain('d1abcd.cloudfront.net');
    const ordinary = 'artifacts/sess-1/clip.mp4';
    await resolveArtifactUrls([M2C_REF, ordinary]);
    const back = unresolveArtifactUrls({
      a: presigner.urlFor(M2C_REF),
      b: presigner.urlFor(ordinary),
    });
    expect(back.a).toBe(M2C_REF);
    // an ordinary key still unresolves to the CDN identifier
    expect(back.b).toBe(`${CDN}/${ordinary}`);
  });

  it('keeps its original string when the proxy refuses it (no broken rewrite)', async () => {
    presigner.notFound.add(M2C_REF);
    const resolved = await resolveArtifactUrlsDeep({ tracks: [{ src: M2C_REF }] });
    expect(resolved.tracks[0].src).toBe(M2C_REF);
  });
});

describe('projects-session output keys (asset-lineage Phase 3 (a))', () => {
  const PID = 'p' + '7'.repeat(32);
  const SID = '4f9d0d02-1111-4222-8333-444455556666';
  const KEY = `projects/${PID}/sessions/${SID}/output/clip.mp4`;

  it('artifactKeyOf maps the bare, CDN and /api/artifact forms; never a project ASSET key', () => {
    expect(artifactKeyOf(KEY)).toBe(KEY);
    expect(artifactKeyOf(`${CDN}/${KEY}?v=1`)).toBe(KEY);
    expect(artifactKeyOf(`/api/artifact?key=${encodeURIComponent(KEY)}`)).toBe(KEY);
    expect(artifactKeyOf(`projects/${PID}/video:x/a.mp4`)).toBeNull();
    expect(artifactKeyOf(`${CDN}/projects/${PID}/video:x/a.mp4`)).toBeNull();
    expect(artifactKeyOf(`${CDN}/projects/${PID}/sessions/${SID}/render/m.json`)).toBeNull();
  });

  it('resolves a projects-session output through /api/artifact-urls', async () => {
    const out = await resolveArtifactUrls([KEY]);
    expect(JSON.parse(authFetch.mock.calls[0][1].body)).toEqual({ keys: [KEY] });
    expect(out.get(KEY)?.url).toBe(presigner.urlFor(KEY));
  });
});

describe('resolveArtifactUrlsInHtml — projects-session outputs', () => {
  it('substitutes a session-output CDN URL inside agent HTML', async () => {
    const KEY = `projects/p${'9'.repeat(32)}/sessions/4f9d0d02-1111-4222-8333-444455556666/output/panel.png`;
    const out = await resolveArtifactUrlsInHtml(`<img src="${CDN}/${KEY}">`);
    expect(presigner.calls[0].keys).toEqual([KEY]);
    expect(out).toContain(presigner.urlFor(KEY).replace(/&/g, '&amp;'));
  });
});

// Staging 2026-10-09 (session 9d649e32): an analysis manifest whose source is a PROJECT file names it as
// `https://<artifacts CDN>/projects/<pid>/video:<uuid>/<file>` (the agent's stable-ref rewrite). With the
// CDN private that 403s and the video never loads ("Generic" tab, MEDIA_ERR_SRC_NOT_SUPPORTED). It must
// resolve through the asset-id-gated Asset_URL_Route, not `/api/artifact-urls` (which never serves it).
describe('project ASSET source keys', () => {
  const PID = 'p' + 'a'.repeat(32);
  const AID = 'video:f7aa8ecd-43d2-4089-9949-3cc0ed4b8ca9';
  const KEY = `projects/${PID}/${AID}/clip.mp4`;
  const CDN_URL = `${CDN}/projects/${PID}/video%3Af7aa8ecd-43d2-4089-9949-3cc0ed4b8ca9/clip.mp4`;
  const SIGNED = 'https://skills-bucket.s3.us-west-2.amazonaws.com/projects/x/clip.mp4?X-Amz-Signature=s';

  beforeEach(() => {
    authFetch.mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === 'string' ? input : String(input);
      if (url === `/projects/${PID}/assets/urls`) {
        const { asset_ids } = JSON.parse(String(init?.body));
        expect(asset_ids).toEqual([AID]);
        return new Response(JSON.stringify({
          urls: { [AID]: { source: { url: SIGNED, expiresAt: Math.floor(Date.now() / 1000) + 3600 } } },
          errors: {},
        }), { status: 200, headers: { 'content-type': 'application/json' } });
      }
      return presigner.handle(input, init);
    });
  });

  it('stays out of artifactKeyOf, but a manifest resolves it through the Asset_URL_Route', async () => {
    expect(artifactKeyOf(CDN_URL)).toBeNull();
    const manifest = { tracks: [{ type: 'video', src: CDN_URL }, { type: 'audio', src: `${CDN}/artifacts/s/output/a.wav` }] };
    const out = await resolveArtifactUrlsDeep(manifest);
    expect(out.tracks[0].src).toBe(SIGNED);
    expect(out.tracks[1].src).toBe(presigner.urlFor('artifacts/s/output/a.wav'));
    // The /api/artifact-urls batch never carries the asset key.
    expect(presigner.calls.flatMap((c) => c.keys)).toEqual(['artifacts/s/output/a.wav']);
    // The re-sign hook can map the presigned URL back to its key.
    expect(keyForUrl(SIGNED)).toBe(KEY);
    expect(keyForUrl(CDN_URL)).toBe(KEY);
  });

  it('keeps the original identifier when the route refuses it', async () => {
    authFetch.mockImplementation(async () => new Response(JSON.stringify({ urls: {}, errors: { [AID]: { code: 'project_not_found' } } }), { status: 200 }));
    const out = await resolveArtifactUrlsDeep({ src: CDN_URL });
    expect(out.src).toBe(CDN_URL);
  });

  it('ignores lookalikes that are not a project file key', async () => {
    expect(hasArtifactRefs({ src: `https://evil.example/projects/${PID}/${AID}/clip.mp4` })).toBe(false);
    expect(hasArtifactRefs({ src: `${CDN}/projects/not-a-pid/import/clip.mp4` })).toBe(false);
    expect(hasArtifactRefs({ src: CDN_URL })).toBe(true);
  });

  // Staging 2026-11-14 (IND403 Lab 1): a job tab's source was the import-inbox file, registered in place at
  // `projects/<pid>/import/<name>` — no asset id in the key — so it reached <video> as the private CDN URL (403).
  it('resolves a file registered in place through resolve-locations, then the Asset_URL_Route', async () => {
    const IMPORT_KEY = `projects/${PID}/import/Meridian.mp4`;
    authFetch.mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const body = JSON.parse(String(init?.body));
      if (url === `/projects/${PID}/assets/resolve-locations`) {
        expect(body.locations).toEqual([IMPORT_KEY]);
        return new Response(JSON.stringify({ locations: { [IMPORT_KEY]: AID } }), { status: 200 });
      }
      if (url === `/projects/${PID}/assets/urls`) {
        expect(body.asset_ids).toEqual([AID]);
        return new Response(JSON.stringify({ urls: { [AID]: { source: { url: SIGNED, expiresAt: Math.floor(Date.now() / 1000) + 3600 } } } }), { status: 200 });
      }
      return presigner.handle(input, init);
    });
    const out = await resolveArtifactUrlsDeep({ tracks: [{ type: 'video', src: `${CDN}/${IMPORT_KEY}` }] });
    expect(out.tracks[0].src).toBe(SIGNED);
    expect(presigner.calls).toHaveLength(0);
  });
});

// Staging 2026-10-10 (sessions 2dba61af / c700c381): a `render-video` manifest and a generated-image
// gallery bake the SOURCE as a RAW PRESIGNED own-bucket S3 URL (every analysis render instead stores
// the artifacts-CDN identifier handled above). The 1-hour token expires, the video times out and the
// images 403, and `artifactKeyOf`/`projectAssetKeyOf` refuse an amazonaws host — so the deep resolver
// never re-signed it. `s3RestArtifactKeyOf` recovers the key from the path so the SAME gated routes
// re-resolve it at view time, on refresh, and on copy.
describe('raw presigned own-bucket S3 source (the render-video / image-gallery bake)', () => {
  const BUCKET = 'mediaworks-harness-skills-store-100000000002-us-west-2';
  const PID = 'p' + 'a'.repeat(32);
  const VID = 'video:6865ef88-8411-4629-898f-618ee72d080f';
  const VKEY = `projects/${PID}/${VID}/Netflix_Open_Content_Meridian.mp4`;
  // Virtual-hosted, path-encoded `video%3A…`, with an X-Amz signature query (exactly the baked shape).
  const PRESIGNED_VIDEO = `https://${BUCKET}.s3.us-west-2.amazonaws.com/projects/${PID}/video%3A6865ef88-8411-4629-898f-618ee72d080f/Netflix_Open_Content_Meridian.mp4?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Signature=abc`;
  const AKEY = `projects/${PID}/image:4eef43c5-bb4f-4fea-be9a-0da086d31ecc/sam.webp`;
  const PRESIGNED_IMAGE = `https://${BUCKET}.s3.us-west-2.amazonaws.com/projects/${PID}/image%3A4eef43c5-bb4f-4fea-be9a-0da086d31ecc/sam.webp?X-Amz-Signature=z`;
  // An own-bucket presigned ARTIFACTS key (a session-output clip baked presigned) → /api/artifact-urls.
  const ARTKEY = 'artifacts/2dba61af/output/clip.mp4';
  const PRESIGNED_ART = `https://${BUCKET}.s3.us-west-2.amazonaws.com/artifacts/2dba61af/output/clip.mp4?X-Amz-Signature=q`;
  const FRESH_VIDEO = 'https://x.s3.us-west-2.amazonaws.com/fresh-video?X-Amz-Signature=new';
  const FRESH_IMAGE = 'https://x.s3.us-west-2.amazonaws.com/fresh-image?X-Amz-Signature=new';

  it('recovers the project-asset / artifact key from a presigned own-bucket URL; refuses foreign & m2c', () => {
    expect(s3RestArtifactKeyOf(PRESIGNED_VIDEO)).toBe(VKEY);
    expect(s3RestArtifactKeyOf(PRESIGNED_IMAGE)).toBe(AKEY);
    expect(s3RestArtifactKeyOf(PRESIGNED_ART)).toBe(ARTKEY);
    // Path-style (bucket as first path segment) resolves too.
    expect(s3RestArtifactKeyOf(`https://s3.us-west-2.amazonaws.com/${BUCKET}/${VKEY}?X-Amz-Signature=p`)).toBe(VKEY);
    // A presigned m2c-proxy object is NOT a resolvable key shape → left for the stable m2c reference.
    expect(s3RestArtifactKeyOf('https://m2c-proxy.s3.us-west-2.amazonaws.com/proxy.mp4?X-Amz-Signature=keep')).toBeNull();
    // Not an S3 host, or not a resolvable key path.
    expect(s3RestArtifactKeyOf(`${CDN}/${VKEY}`)).toBeNull();
    expect(s3RestArtifactKeyOf(`https://${BUCKET}.s3.us-west-2.amazonaws.com/users/secret.json?X-Amz-Signature=z`)).toBeNull();
  });

  it('keyForUrl maps the baked presigned URL so the media re-sign hook can recover it', () => {
    expect(keyForUrl(PRESIGNED_VIDEO)).toBe(VKEY);
    expect(keyForUrl(PRESIGNED_IMAGE)).toBe(AKEY);
    expect(keyForUrl(PRESIGNED_ART)).toBe(ARTKEY);
  });

  it('the deep resolver re-signs a baked presigned project-asset source at view time', async () => {
    authFetch.mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === 'string' ? input : String(input);
      if (url === `/projects/${PID}/assets/urls`) {
        const { asset_ids } = JSON.parse(String(init?.body));
        const out: Record<string, { source: { url: string; expiresAt: number } }> = {};
        const exp = Math.floor(Date.now() / 1000) + 3600;
        for (const id of asset_ids) {
          if (id === VID) out[id] = { source: { url: FRESH_VIDEO, expiresAt: exp } };
          if (id === 'image:4eef43c5-bb4f-4fea-be9a-0da086d31ecc') out[id] = { source: { url: FRESH_IMAGE, expiresAt: exp } };
        }
        return new Response(JSON.stringify({ urls: out, errors: {} }), { status: 200, headers: { 'content-type': 'application/json' } });
      }
      return presigner.handle(input, init);
    });
    // The render-video manifest (track src) and the generated-image gallery (images[].src).
    const manifest = {
      tracks: [{ type: 'video', src: PRESIGNED_VIDEO }],
      images: [{ src: PRESIGNED_IMAGE }, { src: PRESIGNED_ART }],
    };
    expect(hasArtifactRefs(manifest)).toBe(true);
    const out = await resolveArtifactUrlsDeep(manifest);
    expect(out.tracks[0].src).toBe(FRESH_VIDEO);
    expect(out.images[0].src).toBe(FRESH_IMAGE);
    // The own-bucket presigned ARTIFACTS key went through the /api/artifact-urls batch.
    expect(out.images[1].src).toBe(presigner.urlFor(ARTKEY));
    expect(presigner.calls.flatMap((c) => c.keys)).toEqual([ARTKEY]);
    // The freshly-signed URLs map back to their keys for the next re-sign.
    expect(keyForUrl(FRESH_VIDEO)).toBe(VKEY);
    expect(keyForUrl(FRESH_IMAGE)).toBe(AKEY);
  });

  it('keeps the baked presigned URL when the route refuses the asset (never worse than today)', async () => {
    authFetch.mockImplementation(async () => new Response(JSON.stringify({ urls: {}, errors: { [VID]: { code: 'project_not_found' } } }), { status: 200 }));
    const out = await resolveArtifactUrlsDeep({ tracks: [{ type: 'video', src: PRESIGNED_VIDEO }] });
    expect(out.tracks[0].src).toBe(PRESIGNED_VIDEO);
  });
});
