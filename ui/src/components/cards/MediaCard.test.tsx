/** @vitest-environment jsdom */
import React from 'react';
import { describe, it, expect } from 'vitest';
import { render, fireEvent, screen } from '@testing-library/react';
import '@testing-library/jest-dom';
import { MediaCard, FIT_MAX_HEIGHT_PX } from './MediaCard';

describe('MediaCard menu', () => {
  it('lifts the card above its neighbours while the menu is open', () => {
    render(<ul><MediaCard kind="video" title="a.mp4" onPreview={() => {}} menu={[{ label: 'Save to folder', onClick: () => {} }]} /></ul>);
    const card = screen.getByTestId('media-card');
    expect(card.style.zIndex).toBe('');
    fireEvent.click(screen.getByLabelText('More for a.mp4'));
    expect(screen.getByRole('menu')).toBeInTheDocument();
    expect(Number(card.style.zIndex)).toBeGreaterThan(0);
  });
});

describe('MediaCard fitWidth (chat images)', () => {
  it('fits the image by its longer side, never cropped to 16:9 and never enlarged', () => {
    render(<p><MediaCard as="span" kind="image" title="wide.png" thumb="https://x/wide.png" fitWidth onPreview={() => {}} /></p>);
    const card = screen.getByTestId('media-card');
    expect(card).toHaveAttribute('data-fit', 'width');
    // The card hugs the picture, up to the whole bubble (not the 380px card cap).
    expect(card.style.width).toBe('fit-content');
    expect(card.style.maxWidth).toBe('100%');
    const img = screen.getByAltText('wide.png') as HTMLImageElement;
    expect(img.style.objectFit).not.toBe('cover');
    // Auto size with both maxima: a wide image stops at the bubble's width, a tall one at the height cap, and a
    // small one (a 204×271 face crop) stays at its own size instead of being stretched to the bubble's width.
    expect(img.style.width).toBe('auto');
    expect(img.style.height).toBe('auto');
    expect(img.style.maxWidth).toBe('100%');
    expect(img.style.maxHeight).toBe(`${FIT_MAX_HEIGHT_PX}px`);
    expect(img.style.position).toBe(''); // in flow: the picture sets the box height
    expect((card.querySelector('[data-card-media]') as HTMLElement).style.aspectRatio).toBe('auto');
  });

  it('keeps the 16:9 box while the image is still loading, and without fitWidth', () => {
    const { rerender } = render(<p><MediaCard as="span" kind="image" title="a.png" thumb={null} fitWidth onPreview={() => {}} /></p>);
    expect(screen.getByTestId('media-card')).not.toHaveAttribute('data-fit');
    rerender(<p><MediaCard as="span" kind="image" title="a.png" thumb="https://x/a.png" onPreview={() => {}} /></p>);
    expect(screen.getByTestId('media-card')).not.toHaveAttribute('data-fit');
    expect((screen.getByAltText('a.png') as HTMLImageElement).style.objectFit).toBe('cover');
  });
});
