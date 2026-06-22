import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeRaw from "rehype-raw";
import rehypeSanitize, { defaultSchema } from "rehype-sanitize";

const schema = {
  ...defaultSchema,
  tagNames: [
    ...(defaultSchema.tagNames || []), 
    'br', 'details', 'summary', 'kbd', 'mark', 'u', 'sub', 'sup'
  ],
};

interface Props {
  content: string;
  isStreaming: boolean;
}

export default function TextResponse({ content, isStreaming }: Props) {
  if (!content && !isStreaming) return null;

  return (
    <div className="text-[#1a1c1c] pt-1 font-sans">
      <div className="prose prose-sm max-w-none prose-p:my-1 prose-headings:my-2 prose-ul:my-1 prose-li:my-0">
        <ReactMarkdown
          remarkPlugins={[remarkGfm]}
          rehypePlugins={[rehypeRaw, [rehypeSanitize, schema]]}
          components={{
            table: ({ ...props }) => (
              <div className="overflow-x-auto my-6 rounded-xl border border-[#e2e8f0] shadow-sm">
                <table className="w-full border-collapse text-sm bg-white" {...props} />
              </div>
            ),
            thead: ({ ...props }) => <thead className="bg-[#f8fafd] border-b border-[#e2e8f0]" {...props} />,
            th: ({ ...props }) => (
              <th className="py-3.5 px-4 text-left font-bold text-[#424656] text-[11px] uppercase tracking-wider whitespace-nowrap" {...props} />
            ),
            tbody: ({ ...props }) => <tbody className="divide-y divide-[#f1f5f9]" {...props} />,
            tr: ({ ...props }) => <tr className="hover:bg-[#fcfdfd] transition-colors" {...props} />,
            td: ({ ...props }) => <td className="py-3 px-4 text-[#1a1c1c] leading-relaxed" {...props} />,
            a: ({ ...props }) => <a className="text-[#0050cb] hover:underline font-medium" {...props} />,
            code: ({ className, children, ...props }: any) => {
              const isBlock = String(children).includes('\n') || /language-(\w+)/.test(className || '');
              return isBlock ? (
                <code className="block bg-[#1a1c1c] text-white p-4 rounded-lg text-sm overflow-x-auto my-4 font-mono shadow-inner" {...props}>
                  {children}
                </code>
              ) : (
                <code className="bg-[#f1f5f9] text-[#e11d48] px-1.5 py-0.5 rounded text-[13px] font-mono" {...props}>
                  {children}
                </code>
              );
            },
          }}
        >
          {content}
        </ReactMarkdown>
      </div>
      {isStreaming && (
        <span className="inline-block w-[6px] h-4 bg-[#0050cb] ml-1 animate-pulse align-middle"></span>
      )}
    </div>
  );
}
