import type { Components } from 'react-markdown';
import { ArtifactLink } from './ArtifactLink';
import { ASSET_SCHEME, InlineAssetImage } from './InlineAssetImage';
import { classifyUrl } from './artifactUrl';
import { renderWithIdChips } from './compactIds';
import { CitationRef } from './CitationRef';
import { isProjectCardLink, ProjectLinkCard } from './CreateProjectCard';

/**
 * Shared markdown component overrides for the chat surface. Routes every link and image the agent
 * emits through <ArtifactLink> so media previews inline and artifacts are downloadable — the same
 * primitive used for uploaded-file previews. Prose paragraphs/list items also run through
 * `renderWithIdChips`, folding leaked internal identifiers (ARNs, s3:// URIs, job-ids) into compact
 * copy-chips so the end user never sees account ids / regions / execution ARNs in a sentence.
 * Pass to BOTH <ReactMarkdown> instances in ChatCanvas (live stream + persisted MessageBubble).
 */

const MEDIA_KINDS = new Set(['image', 'video', 'audio']);

/** react-markdown passes `code` children as a string (or a single-element array). Return the plain
 * text only when it is exactly one string node, else null (compound content is never a lone URL). */
function loneText(children: React.ReactNode): string | null {
  if (typeof children === 'string') return children;
  if (Array.isArray(children) && children.length === 1 && typeof children[0] === 'string') {
    return children[0];
  }
  return null;
}

export const markdownComponents: Components = {
  // `[Create project “X”](project:new?name=X)` (older chats) and `[Delete project “X”](project:delete?id=…&name=X)`:
  // cards the user acts on.
  a: ({ href, children }) => typeof href === 'string' && isProjectCardLink(href) ? (
    <ProjectLinkCard href={href} />
  ) : (
    <ArtifactLink href={typeof href === 'string' ? href : undefined}>{children}</ArtifactLink>
  ),
  // `![caption](asset:<id>)` — a project image inline in the answer, with an Expand to the Renderer.
  img: ({ src, alt }) => typeof src === 'string' && src.startsWith(ASSET_SCHEME) ? (
    <InlineAssetImage src={src} alt={typeof alt === 'string' ? alt : undefined} />
  ) : (
    <ArtifactLink src={typeof src === 'string' ? src : undefined} alt={typeof alt === 'string' ? alt : undefined} />
  ),
  // A media URL the model wrapped in backticks (`https://…/reel.mp4`) is an inline CODE span, so
  // remark-gfm never autolinks it and it never reaches the `a` override — the user sees monospace
  // text instead of a player. Detect that one case (inline code whose entire content is a single
  // media URL) and route it through ArtifactLink; everything else stays literal <code>. The guards
  // (no language className, no internal whitespace, http(s) + media extension) keep ordinary inline
  // code — paths, commands, non-media URLs — and fenced code blocks untouched.
  code: ({ className, children, ...props }) => {
    const text = loneText(children);
    if (text && !className) {
      const t = text.trim();
      if (!/\s/.test(t) && /^https?:\/\//i.test(t) && MEDIA_KINDS.has(classifyUrl(t).kind)) {
        return <ArtifactLink href={t} />;
      }
    }
    return <code className={className} {...props}>{children}</code>;
  },
  p: ({ children }) => <p>{renderWithIdChips(children)}</p>,
  li: ({ children }) => <li>{renderWithIdChips(children)}</li>,
  // Citation spans. remarkCitations (added only when a message carries a CitationBlock) projects
  // each parsed Citation to a <span data-mh-citation="<key>">; route those through <CitationRef>,
  // which classifies the key against the block in CitationContext (verified link vs unverified
  // mark). Any other span — none is produced by gfm markdown today — renders unchanged.
  span: ({ node: _node, children, ...props }) => {
    const key = (props as { 'data-mh-citation'?: unknown })['data-mh-citation'];
    if (typeof key === 'string') {
      return <CitationRef citationKey={key}>{children}</CitationRef>;
    }
    return <span {...(props as React.HTMLAttributes<HTMLSpanElement>)}>{children}</span>;
  },
};
