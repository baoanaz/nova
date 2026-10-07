/**
 * **唯一**的 Markdown 渲染入口（D-40 / Module 07 §2.2）。
 *
 * 为什么必须集中：ContextPack → Markdown 的渲染在**服务端**完成（D-21），web 只做包装。
 * 一旦页面各自拼装 `[E1]`、各自处理证据块，就会出现"两份渲染逻辑漂移"——
 * 那正是 D-40 明确禁止的。所以这里只做 CommonMark + GFM（表格）渲染，不解析语义。
 */

import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

export function Markdown({ source }: { source: string }) {
  return (
    <div className="nova-markdown">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          h2: (props) => <h2 className="mt-4 mb-2 text-base font-semibold" {...props} />,
          h3: (props) => <h3 className="mt-3 mb-1 text-sm font-semibold text-ink-primary" {...props} />,
          p: (props) => <p className="my-1 text-sm leading-relaxed" {...props} />,
          code: (props) => (
            <code className="rounded bg-paper-base px-1 py-0.5 font-mono text-xs" {...props} />
          ),
          pre: (props) => (
            <pre
              className="my-2 overflow-x-auto rounded bg-ink-primary p-3 font-mono text-xs text-paper-base"
              {...props}
            />
          ),
          table: (props) => (
            <table className="my-2 w-full border-collapse text-sm" {...props} />
          ),
          th: (props) => (
            <th className="border border-ink-line bg-paper-base px-2 py-1 text-left" {...props} />
          ),
          td: (props) => <td className="border border-ink-line px-2 py-1" {...props} />,
          ul: (props) => <ul className="my-1 list-disc pl-5 text-sm" {...props} />,
          ol: (props) => <ol className="my-1 list-decimal pl-5 text-sm" {...props} />,
        }}
      >
        {source}
      </ReactMarkdown>
    </div>
  );
}
