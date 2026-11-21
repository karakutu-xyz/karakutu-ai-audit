export type TextBlock = { text: string; pre: boolean };

export interface ParsedNode {
  nodeType: number;
  nodeName: string;
  nodeValue: string | null;
  childNodes: ArrayLike<ParsedNode>;
}

export const browserParse = (html: string): ParsedNode =>
  new DOMParser().parseFromString(html, "text/html");

export function looksLikeHtml(content: string): boolean {
  // Detection only: the browser parser owns all HTML interpretation.
  return /<!--|<\/[a-z][^<>]*>|<br[\t\n\f\r ]*\/?>/i.test(content) ||
    /<(?:p|div)(?:\/?>|[\t\n\f\r ][^<>]*>)/i.test(content) ||
    /&(?:[a-z][a-z0-9]*|#[0-9]+|#x[0-9a-f]+);/i.test(content);
}

const hidden = new Set([
  "script", "style", "svg", "math", "noscript", "template", "head", "title",
  "iframe", "object", "embed", "canvas", "select", "button",
]);
const boundaries = new Set([
  "p", "div", "section", "article", "header", "footer", "main", "aside", "nav",
  "blockquote", "figure", "figcaption", "h1", "h2", "h3", "h4", "h5", "h6",
  "ul", "ol", "li", "dl", "dt", "dd", "table", "tr", "pre", "address",
  "details", "summary", "hr", "form", "fieldset",
]);

export function htmlToBlocks(
  content: string,
  parse: (html: string) => ParsedNode,
): TextBlock[] {
  const blocks: TextBlock[] = [];
  let parts: string[] = [];
  let preDepth = 0;
  const rows: number[] = [];
  const flush = () => {
    const joined = parts.join("");
    const text = preDepth ? joined : joined.split("\n")
      .map((line) => line.replace(/\s+/g, " ").trim()).join("\n").trim();
    if (text.trim()) blocks.push({ text, pre: preDepth > 0 });
    parts = [];
  };

  // Entry/exit frames avoid recursion even for deeply nested parsed documents.
  const stack = [{ node: parse(content), exit: false }];
  while (stack.length) {
    const { node, exit } = stack.pop()!;
    if (node.nodeType === 3) {
      const value = node.nodeValue ?? "";
      parts.push(preDepth ? value : value.replace(/\s+/g, " "));
      continue;
    }
    if (![1, 9, 11].includes(node.nodeType)) continue;
    const name = node.nodeName.toLowerCase();
    if (hidden.has(name)) continue;
    const pre = name === "pre" || name === "textarea";
    if (exit) {
      if (pre) {
        if (preDepth === 1) flush();
        preDepth--;
      } else if (!preDepth && boundaries.has(name)) {
        flush();
      }
      if (name === "tr") rows.pop();
      continue;
    }
    if (!preDepth && (pre || boundaries.has(name))) flush();
    if (pre) preDepth++;
    if (name === "tr") rows.push(0);
    if (!preDepth && (name === "td" || name === "th") && rows.length) {
      if (rows[rows.length - 1]++) parts.push(" · ");
    }
    if (name === "br") parts.push("\n");
    stack.push({ node, exit: true });
    for (let index = node.childNodes.length - 1; index >= 0; index--) {
      stack.push({ node: node.childNodes[index], exit: false });
    }
  }
  flush();
  return blocks;
}

// Browser parsers slow down sharply on tens of thousands of nested tags (Chromium needs
// seconds for 50k unclosed <div>s), and this runs during render. Real articles hold a few
// hundred tags; past this limit the page shows the stored text unformatted.
export const MAX_TAG_OPENINGS = 20_000;

function tooManyTags(content: string): boolean {
  const opening = /<[a-z]/gi;
  let count = 0;
  while (opening.exec(content)) {
    if (++count > MAX_TAG_OPENINGS) return true;
  }
  return false;
}

export function articleBlocks(
  content: string,
  isArchive: boolean,
  parse: (html: string) => ParsedNode = browserParse,
): TextBlock[] | null {
  if (!isArchive || !looksLikeHtml(content) || tooManyTags(content)) return null;
  return htmlToBlocks(content, parse);
}
