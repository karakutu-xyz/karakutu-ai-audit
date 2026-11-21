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

// Presentation only: saved article content and audit inputs stay unchanged.
export function articlePlainText(value: string): string {
  if (!/<[a-z][\w:-]*\b/i.test(value)) return value;
  const entities: Record<string, string> = {
    amp: "&", lt: "<", gt: ">", quot: '"', "#39": "'", nbsp: " ",
  };
  return value
    .replace(/<(script|style|svg|noscript)\b[^>]*\/>/gi, "")
    .replace(
      /<(script|style|svg|noscript)\b[^>]*>[\s\S]*?<\/\1\s*>/gi,
      "",
    )
    .replace(/<\/\s*(p|div|li|h[1-6])\s*>|<br\b[^>]*>/gi, "\n")
    .replace(/<[^>]*>/g, "")
    .replace(
      /&(amp|lt|gt|quot|#39|nbsp|#\d+|#x[\da-f]+);/gi,
      (entity, name: string) => {
        const key = name.toLowerCase();
        if (entities[key] !== undefined) return entities[key];
        const hex = key.startsWith("#x");
        const code = Number.parseInt(key.slice(hex ? 2 : 1), hex ? 16 : 10);
        return code >= 0 && code <= 0x10ffff &&
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
