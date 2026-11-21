import type { Run, RunStory } from "../../types";
import { humanize } from "../../format";
import { StatusChip } from "../../components/StatusChip";
import { evidenceSummary, record } from "./evidenceSummary";

export function AuditStory({ summary, run }: {
  summary: RunStory;
  run: Run | null;
}) {
  let footnote = 0;
  return (
    <section className="ruled-section" aria-labelledby="audit-story-title">
      <h2 id="audit-story-title">What the audit did</h2>
      <ol className="audit-story">
        {summary.steps.map((step) => {
          const stored = run?.steps.find((item) => item.name === step.name);
          const evidence = step.status === "completed" && stored
            ? evidenceSummary(stored) : [];
          const reason = record(stored?.output).reason;
          const line = step.line || (
            ["failed", "skipped"].includes(step.status) && typeof reason === "string"
              ? reason : null
          );
          return (
            <li key={step.name}>
              <h3>{step.title}</h3>
              <div className="actions">
                <StatusChip status={step.status} />
                {step.outcome && <span className="help">
                  Outcome: {humanize(step.outcome)}
                </span>}
              </div>
              {line && <p className="story-line">{line}</p>}
              {!!evidence.length && (
                <ol className="story-evidence" aria-label={`${step.title} evidence`}>
                  {evidence.map((item) => {
                    // Footnotes are numbered from 1 across the whole story.
                    const number = ++footnote;
                    return (
                      <li key={number} value={number}>
                        {item.url ? <a href={item.url} target="_blank"
                          rel="noopener noreferrer">{item.text}</a> : item.text}
                        {item.quote && <>: <q>{item.quote}</q></>}
                      </li>
                    );
                  })}
                </ol>
              )}
            </li>
          );
        })}
      </ol>
    </section>
  );
}
