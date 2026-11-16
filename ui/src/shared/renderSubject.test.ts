import { describe, it, expect } from 'vitest';
import { groupBySubject, viewSubjectIds, viewVersionNote } from './renderSubject';

describe('viewSubjectIds — what a view is about, from fields views already carry', () => {
  it('a file: its asset (sourceAssetId or an asset-player ref) and its location', () => {
    expect(viewSubjectIds({ template: 'asset-player', ref: { asset_id: 'video:1' }, sourceLocation: 's3://b/k.mp4' }))
      .toEqual(['asset:video:1', 'loc:s3://b/k.mp4']);
    expect(viewSubjectIds({ template: 'nle-timeline', sourceAssetId: 'video:1' })).toEqual(['asset:video:1']);
  });
  it('a path-shaped sourceKey names a file; a bare file name does not (two uploads can share one)', () => {
    expect(viewSubjectIds({ sourceKey: 'a1b2/clip.mp4' })).toEqual(['sk:a1b2/clip.mp4']);
    expect(viewSubjectIds({ sourceKey: 'clip.mp4' })).toEqual([]);
  });
  it('a gallery of no single file is about its title; a file view never uses its title', () => {
    expect(viewSubjectIds({ template: 'clip-gallery', title: 'Voice audition' })).toEqual(['title:Voice audition']);
    expect(viewSubjectIds({ title: 'clip.mp4', sourceAssetId: 'video:1' })).toEqual(['asset:video:1']);
    expect(viewSubjectIds({ template: 'data-table' })).toEqual([]);
    expect(viewSubjectIds(undefined)).toEqual([]);
  });
});

describe('groupBySubject — one tab per subject', () => {
  it('views sharing an id group, transitively, in input order; groups ordered by their first view', () => {
    const ids = [['loc:A'], ['asset:1'], ['asset:1', 'loc:A'], ['title:G'], [], ['title:G']];
    expect(groupBySubject(ids, (x) => x)).toEqual([[0, 1, 2], [3, 5], [4]]);
  });
  it('a view with no ids is its own tab, even beside another with none', () => {
    expect(groupBySubject([[], []], (x) => x)).toEqual([[0], [1]]);
  });
});

describe('a view that says what it is about', () => {
  it('declared subjectIds win (the next version of a gallery names the gallery it revises)', () => {
    expect(viewSubjectIds({ template: 'clip-gallery', title: 'x · trimmed', subjectIds: ['view:g1', ''] })).toEqual(['view:g1']);
    expect(viewSubjectIds({ template: 'clip-gallery', title: 'x', subjectIds: [] })).toEqual(['title:x']);
  });
});

describe('viewVersionNote — what tells a version apart', () => {
  it("a Moments search: its words, quoted, or the image's name", () => {
    expect(viewVersionNote({ galleryMoments: { query: 'dunk', queryRef: { type: 'text', q: 'slam dunk' } } })).toBe('“slam dunk”');
    expect(viewVersionNote({ galleryMoments: { query: 'night drive' } })).toBe('“night drive”');
    expect(viewVersionNote({ galleryMoments: { queryRef: { type: 'image', match: 'any', images: [{ assetId: 'a', label: 'dock.jpg' }] } } })).toBe('dock.jpg');
  });
  it('anything else: none (the switcher names it by its title)', () => {
    expect(viewVersionNote({ galleryMoments: { query: '  ' } })).toBeUndefined();
    expect(viewVersionNote({ template: 'asset-player', title: 'clip.mp4' })).toBeUndefined();
    expect(viewVersionNote(undefined)).toBeUndefined();
  });
});
