import type { Step } from "../../types";
import { containment } from "../../format";

export function record(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown> : {};
}

export function externalUrl(value: unknown): string | undefined {
  if (typeof value !== "string") return;
  try {
    const url = new URL(value);
    if (["http:", "https:"].includes(url.protocol)) return url.href;
  } catch { /* Historical malformed URLs remain plain text. */ }
}

export interface EvidenceLine {
  text: string;
  url?: string;
  quote?: string;
}

function items(value: unknown) {
  return Array.isArray(value) ? value : [];
}

// Only recognized stored fields are projected; arbitrary record text is not scanned.
export function evidenceSummary(step: Step): EvidenceLine[] {
  const lines: EvidenceLine[] = [];
  function page(value: unknown, prefix = "") {
    const item = typeof value === "string" ? { url: value } : record(value);
    const url = externalUrl(item.url);
    if (!url) return;
    const status = typeof item.status === "number" ? ` · HTTP ${item.status}` : "";
    const measured = containment(item.old_in_new);
    lines.push({
      url,
      text: `${prefix}${new URL(url).host}${status}${measured ? ` · ${measured}` : ""}`,
    });
  }
  const details = record(record(step.output).details);
  for (const event of step.evidence ?? []) {
    if (event.error_type) continue;
    if (event.type === "probe_removed_url") page(event.output);
    if (event.type === "fetch_candidates") {
      const output = record(event.output);
      for (const kind of ["live", "dead", "unreadable"]) {
        for (const item of items(output[kind])) page(item);
      }
    }
  }
  if (step.name === "similarity") {
    for (const candidate of items(details.candidates)) page(candidate);
  }
  if (step.name === "source") {
    if (typeof details.attribution_quote === "string" && details.attribution_quote) {
      lines.push({ text: "Attribution quote", quote: details.attribution_quote });
    }
    const measured = containment(details.self_containment);
    if (measured) lines.push({ text: `Original article · ${measured}` });
    const sourceMeasured = containment(details.source_containment);
    if (sourceMeasured) lines.push({ text: `Cited source · ${sourceMeasured}` });
    page(details.dead_source, "Cited source · ");
  }
  if (step.name === "web_search") {
    for (const match of items(details.matches)) page(match, "Story match · ");
    if (typeof details.search_queries === "number" && details.search_queries > 0 &&
        details.search_queries_failed === details.search_queries) {
      lines.push({ text: "Web search failed; the web was not searched." });
    }
    for (const [key, label] of [
      ["search_queries", "search queries attempted"],
      ["search_queries_failed", "search queries failed"],
      ["web_hits", "search hits"], ["archive_candidates", "archive candidates"],
      ["cited_source_leads", "cited-source leads"],
      ["hits_searched", "total candidates"], ["live_pages_read", "live pages read"],
      ["unreadable_count", "unreadable pages"],
    ]) {
      if (typeof details[key] === "number") {
        lines.push({ text: `${details[key]} ${label}` });
      }
    }
    const measured = containment(details.old_in_new);
    if (measured) lines.push({ text: `Moved copy · ${measured}` });
  }
  if (step.name === "related_removals") {
    for (const value of items(details.members)) {
      const member = record(value);
      const text = [member.title, member.newspaper]
        .filter((part) => typeof part === "string" && part).join(" · ");
      if (text) lines.push({ text, url: externalUrl(member.url) });
    }
    const publishers = Object.keys(record(details.publishers));
    if (publishers.length) {
      lines.push({ text: `${publishers.length} publishers · ${publishers.join(", ")}` });
    }
  }
  return lines.filter((line, index) => lines.findIndex((other) =>
    other.text === line.text && other.url === line.url && other.quote === line.quote,
  ) === index);
}

export function originalIsDead(step: Step | undefined, original: string) {
  // A failed probe or a dead cited source says nothing about the original URL.
  return step?.evidence?.some((event) => {
    if (event.type !== "probe_removed_url" || event.error_type) return false;
    const output = record(event.output);
    return externalUrl(output.url) === externalUrl(original) &&
      !!externalUrl(original) && [404, 410].includes(Number(output.status));
  }) ?? false;
}
