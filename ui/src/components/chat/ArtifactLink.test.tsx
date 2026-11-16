/**
 * @vitest-environment jsdom
 */
import '@testing-library/jest-dom';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';

const authFetch = vi.hoisted(() => vi.fn());
vi.mock('../../auth/authFetch', () => ({ authFetch }));
vi.mock('../../lab-api', () => ({ getConfig: async () => ({ artifactsDomain: '' }) }));

import { ArtifactLink } from './ArtifactLink';
import { __resetArtifactUrlsForTests } from '../../artifacts/artifactUrls';
import { makeFakePresigner, type FakePresigner } from '../../artifacts/testPresigner';

let presigner: FakePresigner;

beforeEach(() => {
  __resetArtifactUrlsForTests();
  presigner = makeFakePresigner();
  authFetch.mockReset();
  authFetch.mockImplementation(presigner.handle);
});
afterEach(() => {
  vi.restoreAllMocks();
});

describe('ArtifactLink', () => {
  // ---- every media file is ONE card: preview area + Preview / Download / Open in side panel ----
  const card = () => screen.getByTestId('chat-asset-card');
  const actionsOf = () => [...card().querySelectorAll('.cap-acts button, [data-card-acts] button')].map((b) => b.getAttribute('aria-label'));

  it('image, video and document render the same card with the same three actions; audio drops only Preview', () => {
    for (const [href, name] of [
      ['https://x.cloudfront.net/artifacts/a.jpg', 'a.jpg'], ['https://x.cloudfront.net/artifacts/b.mp4', 'b.mp4'],
      ['https://x.cloudfront.net/artifacts/d.pdf', 'd.pdf'],
    ]) {
      const { unmount } = render(<ArtifactLink href={href} />);
      expect(actionsOf()).toEqual([`Preview ${name}`, `Download ${name}`, `Open ${name} in the side panel`]);
      unmount();
    }
    render(<ArtifactLink href="https://x.cloudfront.net/artifacts/c.wav" />);
    expect(actionsOf()).toEqual(['Download c.wav', 'Open c.wav in the side panel']);
  });

  it('auto-inlines an image from a trusted host, loading the RESOLVED (presigned) URL', async () => {
    render(<ArtifactLink href="https://x.cloudfront.net/artifacts/panel.jpg" />);
    // Nothing loads the public CDN URL while the identifier is being resolved.
    expect(card().querySelector('img')).toBeNull();
    await waitFor(() => expect(card().querySelector('img')).toHaveAttribute('src', presigner.urlFor('artifacts/panel.jpg')));
    expect(card().querySelector('img')).toHaveAttribute('loading', 'lazy');
  });

  it('Preview shows the image large; Open in side panel hands the identifier URL to the Renderer', async () => {
    const seen: unknown[] = [];
    const on = (e: Event) => seen.push((e as CustomEvent).detail);
    window.addEventListener('lab:render-urls', on);
    render(<ArtifactLink src="https://x.cloudfront.net/artifacts/strip.jpg" alt="Frames 1–8" />);
    fireEvent.click(screen.getByRole('button', { name: 'Preview strip.jpg' }));
    const dialog = screen.getByRole('dialog', { name: /Preview of strip\.jpg/ });
    expect(seen).toEqual([]);                      // viewing is not editing: nothing opened in the side panel
    await waitFor(() => expect(dialog.querySelector('img')).toHaveAttribute('src', presigner.urlFor('artifacts/strip.jpg')));
    fireEvent.click(screen.getByRole('button', { name: /Open in side panel/ }));
    window.removeEventListener('lab:render-urls', on);
    expect(seen).toEqual([{ urls: ['https://x.cloudfront.net/artifacts/strip.jpg'], label: 'Frames 1–8' }]);
    expect(screen.queryByRole('dialog')).toBeNull();
  });

  it('clicking the media opens the preview, and Esc or the backdrop closes it', () => {
    render(<ArtifactLink src="https://v3.fal.media/files/a/out.png" />);
    const media = () => card().querySelector('[data-card-media]') as HTMLElement;
    fireEvent.click(media());
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    fireEvent.keyDown(window, { key: 'Escape' });
    expect(screen.queryByRole('dialog')).toBeNull();
    fireEvent.click(media());
    fireEvent.mouseDown(screen.getByTestId('asset-preview-backdrop'));
    expect(screen.queryByRole('dialog')).toBeNull();
  });

  it('falls back to the original URL when resolution fails', async () => {
    presigner.failStatus = 500;
    render(<ArtifactLink href="https://x.cloudfront.net/artifacts/panel.jpg" />);
    await waitFor(() => expect(card().querySelector('img')).toHaveAttribute('src', 'https://x.cloudfront.net/artifacts/panel.jpg'));
  });

  it('does NOT auto-load an image from an untrusted host — the card asks first', () => {
    render(<ArtifactLink href="https://tracker.evil.com/pixel.png" />);
    expect(card().querySelector('img')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: /Load image/ }));
    // Loads directly once asked (not an artifact identifier, nothing to resolve).
    expect(card().querySelector('img')).toHaveAttribute('src', 'https://tracker.evil.com/pixel.png');
    expect(authFetch).not.toHaveBeenCalled();
  });

  it('loads a fal image directly, without resolution', () => {
    render(<ArtifactLink href="https://v3.fal.media/files/a/out.png" />);
    expect(card().querySelector('img')).toHaveAttribute('src', 'https://v3.fal.media/files/a/out.png');
    expect(authFetch).not.toHaveBeenCalled();
  });

  it('a video card mounts no player (its picture is a hero still); Preview plays the resolved URL', async () => {
    render(<ArtifactLink href="https://x.cloudfront.net/artifacts/clip.mp4" />);
    // No project file behind it here, so the card shows the kind tile; a player per card starved Firefox (IND403).
    await waitFor(() => expect(screen.getByRole('button', { name: 'Preview clip.mp4' })).toBeInTheDocument());
    expect(card().querySelector('video')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Preview clip.mp4' }));
    const video = screen.getByTestId('asset-preview-video');
    expect(video).toHaveAttribute('src', presigner.urlFor('artifacts/clip.mp4'));
    expect(video).not.toHaveAttribute('crossorigin');
  });

  it('an audio card plays in place: a compact player on the resolved URL, nothing downloaded until play', async () => {
    render(<ArtifactLink href="https://x.cloudfront.net/artifacts/stem.wav" />);
    expect(card().querySelector('audio')).toBeNull();        // never the public CDN URL
    await waitFor(() => expect(card().querySelector('audio')).toHaveAttribute('src', presigner.urlFor('artifacts/stem.wav')));
    const audio = card().querySelector('audio')!;
    expect(audio).toHaveAttribute('preload', 'none');
    expect(audio).not.toHaveAttribute('crossorigin');
    expect(card().querySelector('.cap-media')).toBeNull();   // no 16:9 stage, no preview
    expect(screen.queryByRole('button', { name: 'Preview stem.wav' })).toBeNull();
  });

  it('a document card previews the resolved URL in the preview', async () => {
    render(<ArtifactLink href="https://x.cloudfront.net/artifacts/report.pdf" />);
    expect(authFetch).not.toHaveBeenCalled();     // nothing resolved until asked
    fireEvent.click(screen.getByRole('button', { name: 'Preview report.pdf' }));
    await waitFor(() => expect(screen.getByTestId('asset-preview-document')).toHaveAttribute('src', presigner.urlFor('artifacts/report.pdf')));
    expect(card()).toHaveTextContent('PDF');
  });

  it('a document preview falls back to the /api/artifact read-through when resolution fails', async () => {
    presigner.failStatus = 500;
    render(<ArtifactLink href="https://x.cloudfront.net/artifacts/report.pdf" />);
    fireEvent.click(screen.getByRole('button', { name: 'Preview report.pdf' }));
    await waitFor(() => expect(screen.getByTestId('asset-preview-document')).toHaveAttribute('src', '/api/artifact?key=artifacts%2Freport.pdf'));
  });

  it('Download presigns with download: true and navigates to the attachment URL', async () => {
    const clicked: string[] = [];
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) {
      clicked.push(this.href);
    });
    render(<ArtifactLink href="https://x.cloudfront.net/artifacts/report.pdf" />);
    fireEvent.click(screen.getByRole('button', { name: /Download/i }));
    await waitFor(() => expect(clicked).toEqual([presigner.urlFor('artifacts/report.pdf', true)]));
    expect(presigner.calls).toEqual([{ keys: ['artifacts/report.pdf'], download: true }]);
  });

  it('Download falls back to the /api/artifact attachment read-through when resolution fails', async () => {
    presigner.failStatus = 500;
    const fetchSpy = vi.fn(async () => { throw new TypeError('blocked'); });
    vi.stubGlobal('fetch', fetchSpy);
    const open = vi.spyOn(window, 'open').mockImplementation(() => null);
    render(<ArtifactLink href="https://x.cloudfront.net/artifacts/report.pdf" />);
    fireEvent.click(screen.getByRole('button', { name: /Download/i }));
    // The pre-change path: fetch the /api/artifact attachment form (then open it if fetch fails).
    await waitFor(() => expect(fetchSpy).toHaveBeenCalledWith('/api/artifact?key=artifacts%2Freport.pdf&download=1', { mode: 'cors' }));
    await waitFor(() => expect(open).toHaveBeenCalledWith('/api/artifact?key=artifacts%2Freport.pdf&download=1', '_blank', 'noopener,noreferrer'));
  });

  // Staging 2026-10-09 (session e35814f4): a `.json` artifact (shots.json, speaker_manifest.json)
  // classifies as a plain link, and its href was the raw artifacts-CDN URL — a 403 since the CDN
  // went private (Phase 3 (c)). Our own artifact must open through the resolver like a document.
  it('opens a plain link to our own artifact (json) through the resolver, never the CDN URL', async () => {
    const win = { opener: {} as unknown, closed: false, location: { href: '' } };
    vi.spyOn(window, 'open').mockImplementation(() => win as unknown as Window);
    const cdn = 'https://dexamplecdn002.cloudfront.net/artifacts/s1/output/j1/shots.json';
    render(<ArtifactLink href={cdn}>shots.json</ArtifactLink>);
    const a = screen.getByText('shots.json').closest('a')!;
    expect(a).toHaveAttribute('href', '/api/artifact?key=artifacts%2Fs1%2Foutput%2Fj1%2Fshots.json');
    fireEvent.click(a);
    await waitFor(() => expect(win.location.href).toBe(presigner.urlFor('artifacts/s1/output/j1/shots.json')));
  });

  it('renders a plain, safe link for a non-media URL', () => {
    render(<ArtifactLink href="https://example.com/page">docs</ArtifactLink>);
    const a = screen.getByText('docs').closest('a')!;
    expect(a).toHaveAttribute('href', 'https://example.com/page');
    expect(a).toHaveAttribute('target', '_blank');
    expect(a).toHaveAttribute('rel', 'noopener noreferrer');
  });

  it('renders children unchanged when there is no URL', () => {
    render(<ArtifactLink>plain text</ArtifactLink>);
    expect(screen.getByText('plain text')).toBeInTheDocument();
  });
});
