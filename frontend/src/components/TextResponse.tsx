import ReactMarkdown from "react-markdown";

interface Props {
  content: string;
  isStreaming: boolean;
}

export default function TextResponse({ content, isStreaming }: Props) {
  if (!content && !isStreaming) return null;

  return (
    <div className="text-[#1a1c1c] pt-1 font-sans">
      <div className="prose prose-sm max-w-none prose-p:my-1 prose-headings:my-2 prose-ul:my-1 prose-li:my-0">
        <ReactMarkdown>{content}</ReactMarkdown>
      </div>
      {isStreaming && (
        <span className="inline-block w-[6px] h-4 bg-[#0050cb] ml-1 animate-pulse align-middle"></span>
      )}
    </div>
  );
}
