import { useEffect, useMemo, useRef, useState } from "react";
import type { ArticleDetail } from "../../types";
import { articleDate } from "../../format";
import { record } from "../steps/evidenceSummary";
import { articleBlocks, browserParse } from "./articleHtml";

export function ArticleText({ article, parse = browserParse }: {
  article: ArticleDetail;
  parse?: typeof browserParse;
}) {
  const isArchive = !!article.archive_id || article.ref.startsWith("ka-");
  const blocks = useMemo(() => articleBlocks(article.content, isArchive, parse),
    [article.content, isArchive, parse]);
  const [expanded, setExpanded] = useState(false);
  const [long, setLong] = useState(false);
  const text = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const element = text.current;
    if (!element) return;
    const measure = () => {
      const lineHeight = Number.parseFloat(getComputedStyle(element).lineHeight);
      setLong(element.scrollHeight > lineHeight * 12 + 1);
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => observer.disconnect();
  }, [article.content, blocks]);
  const versions = Array.isArray(article.snapshot?.versions)
    ? article.snapshot.versions.map(record) : [];
  return (
    <section className="ruled-section" aria-labelledby="article-text-title">
      <h2 id="article-text-title">The article</h2>
      <p className="help">Removed text</p>
      <div id="removed-text"
        className={`removed-text${expanded ? " expanded" : ""}`}>
        <div ref={text} className="removed-text-body">
          {!blocks ? article.content || "No article text is stored yet."
            : blocks.length ? blocks.map((block, index) => block.pre
              ? <pre key={index}>{block.text}</pre>
              : <p key={index}>{block.text}</p>)
              : "The stored article has no readable text."}
        </div>
      </div>
      {long && <button className="text-button" type="button"
        aria-expanded={expanded} aria-controls="removed-text"
        onClick={() => setExpanded((value) => !value)}>
        {expanded ? "Show less text" : "Show full text"}
      </button>}
      {!!versions.length && (
        <details>
          <summary>Snapshot versions ({versions.length})</summary>
          <ul>{versions.map((version, index) => (
            <li key={index}>
              {typeof version.title === "string" ? version.title : "Untitled snapshot"}
              {" · Checked "}{articleDate(
                typeof version.check_date === "number" ||
                typeof version.check_date === "string" ? version.check_date : null,
              )}
            </li>
          ))}</ul>
        </details>
      )}
    </section>
  );
}
