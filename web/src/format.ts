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
const articleLineEnds = new Set([
  "p", "div", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "blockquote",
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
  let lower: string | undefined;
  while (cursor < value.length) {
    if (hidden) {
      cursor = lower!.indexOf(`</${hidden}`, cursor);
      if (cursor === -1) {
        textStart = value.length;
        break;
      }
    }
    if (value[cursor] !== "<") {
      cursor++;
      continue;
    }
    tagStart.lastIndex = cursor;
    const tag = tagStart.exec(value);
    const name = tag?.[2].toLowerCase() ?? "";
    if (!tag || !articleElements.has(name) ||
        (hidden && (name !== hidden || !tag[1]))) {
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
      if (!hidden) parts.push(value.slice(textStart));
      textStart = value.length;
      break;
    }
    if (value[end] === "<") {
      // Resume here without rescanning the malformed prefix.
      cursor = end;
      continue;
    }
    foundTag = true;
    if (!hidden) parts.push(value.slice(textStart, cursor));
    cursor = end + 1;
    textStart = cursor;
    if (hidden) {
      hidden = "";
    } else if (!tag[1] && hiddenArticleElements.has(name) &&
               value[end - 1] !== "/") {
      hidden = name;
      // ASCII folding preserves offsets even when text contains e.g. U+0130.
      lower ??= value.replace(/[A-Z]/g, (char) => char.toLowerCase());
    } else if (name === "hr") {
      parts.push("\n\n");
    } else if (name === "br" || (tag[1] && articleLineEnds.has(name))) {
      parts.push("\n");
    }
  }
  if (!foundTag) return value;
  parts.push(value.slice(textStart));
  const entities: Record<string, string> = {
    amp: "&", lt: "<", gt: ">", quot: '"', apos: "'", nbsp: " ",
  };
  return parts.join("")
    .replace(
      /&(amp|lt|gt|quot|apos|nbsp|#\d{1,7}|#x[\da-f]{1,6});/gi,
      (entity, name: string) => {
        const key = name.toLowerCase();
        if (entities[key] !== undefined) return entities[key];
        const hex = key.startsWith("#x");
        const code = Number.parseInt(key.slice(hex ? 2 : 1), hex ? 16 : 10);
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
