import type { ReactNode } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import type { Components } from 'react-markdown';

function linkProps(href?: string): React.AnchorHTMLAttributes<HTMLAnchorElement> {
  return {
    href,
    target: '_blank',
    rel: 'noopener noreferrer',
  };
}

// Module-level component map: created once, not per render.
const components: Components = {
  a: ({ node: _node, children, ...props }) => (
    <a {...props} {...linkProps(props.href)}>
      {children}
    </a>
  ),
  table: ({ node: _node, children }) => (
    <div className="agent-md__table-wrap">
      <table>{children}</table>
    </div>
  ),
};

/**
 * Safe GFM markdown renderer for assistant replies.
 * Raw HTML is NOT rendered: ReactMarkdown never injects raw HTML unless
 * rehype-raw is used, and we deliberately do not (no dangerouslySetInnerHTML).
 */
export default function AgentMarkdown({ text }: { text: string }): ReactNode {
  return (
    <div className="agent-md">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={components}
        skipHtml
        disallowedElements={['script', 'style', 'iframe', 'object', 'embed']}
        unwrapDisallowed
      >
        {text}
      </ReactMarkdown>
    </div>
  );
}
