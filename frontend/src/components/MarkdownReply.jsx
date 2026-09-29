import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';

/**
 * Renders agent reply text as sanitized markdown (tables, lists, code, emphasis).
 * react-markdown never uses dangerouslySetInnerHTML, so raw HTML in the source
 * text is rendered as plain text rather than executed.
 */
export default function MarkdownReply({ text, className = '' }) {
  return (
    <div className={`markdown-reply ${className}`}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          a: (props) => <a {...props} target="_blank" rel="noopener noreferrer" className="underline text-sky-400 hover:text-sky-300" />,
          table: (props) => (
            <div className="overflow-x-auto my-2">
              <table {...props} className="border-collapse text-[11px] w-full" />
            </div>
          ),
          th: (props) => <th {...props} className="border border-white/10 px-2 py-1 bg-white/5 text-left font-semibold" />,
          td: (props) => <td {...props} className="border border-white/10 px-2 py-1 align-top" />,
          code: ({ inline, ...props }) =>
            inline
              ? <code {...props} className="bg-white/10 rounded px-1 py-0.5 text-[11px] font-mono" />
              : <code {...props} className="block bg-black/40 rounded-lg p-2 my-1 text-[11px] font-mono overflow-x-auto" />,
          p: (props) => <p {...props} className="mb-1 last:mb-0" />,
          ul: (props) => <ul {...props} className="list-disc pl-4 my-1 space-y-0.5" />,
          ol: (props) => <ol {...props} className="list-decimal pl-4 my-1 space-y-0.5" />,
        }}
      >
        {text}
      </ReactMarkdown>
    </div>
  );
}
