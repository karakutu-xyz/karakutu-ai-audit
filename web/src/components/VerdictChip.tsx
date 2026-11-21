// The single mapping for verdict language and presentation throughout the UI.
export const verdicts: Record<string, { label: string; tone: string }> = {
  POSSIBLE_CENSORSHIP: { label: "Flagged for human review", tone: "review" },
  AUDIT_FAILED: { label: "Audit failed", tone: "failure" },
  NOT_REMOVED: { label: "Not removed", tone: "not-removed" },
  MOVED: { label: "Moved", tone: "moved" },
  LIKELY_MOVED: { label: "Likely moved", tone: "likely-moved" },
  FOUND_ONLINE: { label: "Found online", tone: "found" },
  INCONCLUSIVE: { label: "Inconclusive", tone: "inconclusive" },
  LIKELY_LOW_PUBLIC_INTEREST: {
    label: "Likely low public interest",
    tone: "low-interest",
  },
};

export const verdictLabel = (value: string) => verdicts[value]?.label ?? value;

export function VerdictChip({ verdict }: { verdict: string | null }) {
  if (!verdict) return <span className="help">Pending verdict</span>;
  const item = verdicts[verdict];
  return (
    <span className={`chip verdict-${item?.tone ?? "inconclusive"}`}>
      {verdictLabel(verdict)}
    </span>
  );
}
