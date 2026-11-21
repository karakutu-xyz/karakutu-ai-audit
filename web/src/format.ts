import type { Run, RunSummary } from "./types";

export const humanize = (value: string) =>
  value
    .toLowerCase()
    .replaceAll("_", " ")
    .replace(/^./, (s) => s.toUpperCase());

export const pending = (status: string) =>
  ["queued", "running"].includes(status);

export const date = (value: string) =>
  new Date(value).toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });

export function duration(ms: number | null) {
  if (ms == null || !Number.isFinite(ms)) return "—";
  const seconds = Math.max(0, ms) / 1000;
  if (seconds < 10) return `${seconds.toFixed(1)} s`;
  if (seconds < 60) return `${Number(seconds.toFixed(1))} s`;
  const wholeSeconds = Math.round(seconds);
  return `${Math.floor(wholeSeconds / 60)} min ${wholeSeconds % 60} s`;
}

export const runDuration = (run: RunSummary) =>
  run.finished_at
    ? duration(
        Math.max(0, Date.parse(run.finished_at) - Date.parse(run.created_at)),
      )
    : pending(run.status)
      ? "In progress"
      : "—";

export const originLabel = (run: Pick<RunSummary, "category" | "origin">) =>
  run.origin === "article"
    ? "Manual audit"
    : run.category === "benchmark"
      ? "Benchmark"
      : run.origin === "api"
        ? "API"
        : humanize(run.origin);

// Compare display text only; the saved title and label remain untouched.
export function ledgerLabel(run: RunSummary) {
  const label = run.label?.trim() ?? "";
  const normalize = (text: string) =>
    text.trim().replace(/\s+/g, " ").toLowerCase();
  const title = normalize(run.article_title);
  const normalized = normalize(label);
  if (!normalized || normalized === title) return "";
  if (title && normalized.endsWith(title)) {
    const prefix = normalized.slice(0, -title.length).trimEnd();
    if (/[·|:–—-]$/.test(prefix)) return "";
  }
  return label;
}

export function runModel(run: Run) {
  if (typeof run.model?.name === "string" && run.model.name.trim()) {
    return run.model.name;
  }
  return (
    [...new Set(run.steps.map((step) => step.model).filter(Boolean))].join(", ") ||
    "Not recorded"
  );
}

export function articleDate(value: string | number | null) {
  if (value === null || value === "") return "—";
  const timestamp = typeof value === "number" ? value * 1000 : value;
  return Number.isNaN(new Date(timestamp).getTime()) ? String(value) : date(
    typeof timestamp === "number" ? new Date(timestamp).toISOString() : timestamp,
  );
}

const articleElements = new Set((
  "p br div span a b i u em strong small sub sup li ul ol " +
  "h1 h2 h3 h4 h5 h6 blockquote figure figcaption img picture source " +
  "section article header footer aside main nav table thead tbody tr td th " +
  "hr iframe video audio time mark cite q abbr font center script style svg " +
  "noscript button form input label select option textarea"
).split(" "));
const hiddenArticleElements = new Set(["script", "style", "svg", "noscript"]);
// Opening or closing any of these starts a new line (paragraphs may be unclosed).
const articleBlocks = new Set((
  "p div li ul ol h1 h2 h3 h4 h5 h6 tr table thead tbody blockquote " +
  "section article header footer aside main nav figure figcaption"
).split(" "));

// Named entities seen in news markup (case-sensitive, as in HTML): all Latin-1
// names, common punctuation and the Turkish letters. Unknown names stay as text.
const articleEntities = new Map<string, string>([
  ...(
    "nbsp iexcl cent pound curren yen brvbar sect uml copy ordf laquo not shy " +
    "reg macr deg plusmn sup2 sup3 acute micro para middot cedil sup1 ordm " +
    "raquo frac14 frac12 frac34 iquest Agrave Aacute Acirc Atilde Auml Aring " +
    "AElig Ccedil Egrave Eacute Ecirc Euml Igrave Iacute Icirc Iuml ETH " +
    "Ntilde Ograve Oacute Ocirc Otilde Ouml times Oslash Ugrave Uacute Ucirc " +
    "Uuml Yacute THORN szlig agrave aacute acirc atilde auml aring aelig " +
    "ccedil egrave eacute ecirc euml igrave iacute icirc iuml eth ntilde " +
    "ograve oacute ocirc otilde ouml divide oslash ugrave uacute ucirc uuml " +
    "yacute thorn yuml"
  ).split(" ").map((name, index): [string, string] =>
    [name, String.fromCharCode(160 + index)]),
  ...Object.entries({
    amp: 38, AMP: 38, lt: 60, LT: 60, gt: 62, GT: 62, quot: 34, QUOT: 34,
    apos: 39, OElig: 338, oelig: 339, Scaron: 352, scaron: 353, Yuml: 376,
    fnof: 402, circ: 710, tilde: 732, ensp: 8194, emsp: 8195, thinsp: 8201,
    zwnj: 8204, zwj: 8205, lrm: 8206, rlm: 8207, ndash: 8211, mdash: 8212,
    lsquo: 8216, rsquo: 8217, sbquo: 8218, ldquo: 8220, rdquo: 8221,
    bdquo: 8222, dagger: 8224, Dagger: 8225, bull: 8226, hellip: 8230,
    permil: 8240, prime: 8242, Prime: 8243, lsaquo: 8249, rsaquo: 8250,
    euro: 8364, trade: 8482, Gbreve: 286, gbreve: 287, Idot: 304,
    imath: 305, inodot: 305, Scedil: 350, scedil: 351,
  }).map(([name, code]): [string, string] =>
    [name, String.fromCharCode(code)]),
  ["nbsp", " "],
]);

// Presentation only: saved article content and audit inputs stay unchanged.
export function articlePlainText(value: string): string {
  // A tag name ends at whitespace, "/" or ">" only, so "x<p+1" stays text.
  const tagStart = /<(\/?)([a-z][a-z0-9-]{0,30})(?=[\s/>])/iy;
  const parts: string[] = [];
  let cursor = 0;
  let textStart = 0;
  let foundTag = false;
  let hidden = "";
  // SVG nests: count depth, and cache the next open/close so each region is
  // searched once (-1: none left; any other value below the cursor: stale).
  let depth = 0;
  let openAt = -2;
  let closeAt = -2;
  let lower: string | undefined;
  let atBreak = true;
  let unclosedComment = false;
  const text = (end: number) => {
    const slice = value.slice(textStart, end);
    if (slice.trim()) atBreak = false;
    parts.push(slice);
  };
  const lineBreak = () => {
    if (!atBreak) parts.push("\n");
    atBreak = true;
  };
  while (cursor < value.length) {
    if (hidden) {
      if (closeAt !== -1 && closeAt < cursor) {
        closeAt = lower!.indexOf(`</${hidden}`, cursor);
      }
      if (hidden === "svg" && openAt !== -1 && openAt < cursor) {
        openAt = lower!.indexOf("<svg", cursor);
      }
      const next = hidden === "svg" && openAt !== -1 &&
        (closeAt === -1 || openAt < closeAt) ? openAt : closeAt;
      if (next === -1) {
        textStart = value.length;
        break;
      }
      cursor = next;
    }
    if (value[cursor] !== "<") {
      cursor++;
      continue;
    }
    if (!hidden && !unclosedComment && value.startsWith("<!--", cursor)) {
      // Comments hide their contents, tags included. One search per comment.
      const close = value.indexOf("-->", cursor + 4);
      if (close === -1) {
        unclosedComment = true;
        cursor++;
        continue;
      }
      text(cursor);
      foundTag = true;
      cursor = close + 3;
      textStart = cursor;
      continue;
    }
    tagStart.lastIndex = cursor;
    const tag = tagStart.exec(value);
    const name = tag?.[2].toLowerCase() ?? "";
    if (!tag || !articleElements.has(name) ||
        (hidden && (name !== hidden || (!tag[1] && hidden !== "svg")))) {
      cursor++;
      continue;
    }
    let end = cursor + tag[0].length;
    let quote = "";
    for (; end < value.length; end++) {
      const char = value[end];
      if (quote) {
        if (char === quote) quote = "";
      } else if (char === '"' || char === "'") {
        quote = char;
      } else if (char === "<" || char === ">") {
        break;
      }
    }
    if (end === value.length) {
      if (!hidden) text(value.length);
      textStart = value.length;
      break;
    }
    if (value[end] === "<") {
      // Resume here without rescanning the malformed prefix.
      cursor = end;
      continue;
    }
    foundTag = true;
    if (!hidden) text(cursor);
    cursor = end + 1;
    textStart = cursor;
    if (hidden) {
      if (!tag[1]) {
        if (value[end - 1] !== "/") depth++;
      } else if (--depth === 0) {
        hidden = "";
      }
    } else if (!tag[1] && hiddenArticleElements.has(name) &&
               value[end - 1] !== "/") {
      hidden = name;
      depth = 1;
      openAt = closeAt = -2;
      // ASCII folding preserves offsets even when text contains e.g. U+0130.
      lower ??= value.replace(/[A-Z]/g, (char) => char.toLowerCase());
    } else if (name === "hr") {
      parts.push("\n\n");
      atBreak = true;
    } else if (name === "br") {
      parts.push("\n");
      atBreak = true;
    } else if (articleBlocks.has(name)) {
      lineBreak();
    } else if ((name === "td" || name === "th") && !tag[1] && !atBreak) {
      // Separate table cells so "12" and "34" never read as "1234".
      parts.push(" ");
    }
  }
  if (!foundTag) return value;
  parts.push(value.slice(textStart));
  return parts.join("")
    .replace(
      /&([a-z][a-z\d]{1,31}|#\d{1,7}|#x[\da-f]{1,6});/gi,
      (entity, name: string) => {
        if (name[0] !== "#") return articleEntities.get(name) ?? entity;
        const hex = name[1] === "x" || name[1] === "X";
        const code = Number.parseInt(name.slice(hex ? 2 : 1), hex ? 16 : 10);
        return code > 0 && code <= 0x10ffff &&
          !(code >= 0xd800 && code <= 0xdfff)
          ? String.fromCodePoint(code) : entity;
      },
    )
    .split(/\r\n?|\n/)
    .map((line) => line.trim())
    .join("\n")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}

export function shortWorkflow(id: string) {
  return id.length > 15 ? `${id.slice(0, 15)}…` : id;
}

export function containment(value: unknown) {
  return typeof value === "number" && Number.isFinite(value) &&
    value >= 0 && value <= 1 ? `${(value * 100).toFixed(1)}% containment` : "";
}
