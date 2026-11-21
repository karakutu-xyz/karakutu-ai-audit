import type { ReactNode } from "react";

export function FactsGrid({
  facts,
}: {
  facts: { label: string; value: ReactNode }[];
}) {
  return (
    <dl className="facts-grid">
      {facts.map(({ label, value }) => (
        <div key={label}>
          <dt>{label}</dt>
          <dd>{value}</dd>
        </div>
      ))}
    </dl>
  );
}
