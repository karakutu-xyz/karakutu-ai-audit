import { Fragment, useState } from "react";
import type { Run, Workflow } from "../../types";
import { duration } from "../../format";
import { StatusChip } from "../../components/StatusChip";
import { Json } from "../../components/Json";
import { Icon } from "../../components/Icon";
import { Evidence } from "./Evidence";
import { ModelCalls } from "./ModelCalls";

const steps = [
  ["classification", "Classify"],
  ["similarity", "Find a moved copy"],
  ["relevance", "Screen public interest"],
  ["related_removals", "Check related removals"],
  ["source", "Verify the source"],
  ["web_search", "Find independent evidence"],
];

export function StepTable({
  run,
  workflow,
}: {
  run: Run;
  workflow: Workflow | null;
}) {
  const [expanded, setExpanded] = useState<string[]>([]);
  // Preserve any additional historical records after the six canonical steps.
  const definitions = [
    ...steps.map(([name, title]) => ({ name, title })),
    ...(workflow?.definition.steps ?? []).filter(
      (def) => !steps.some(([name]) => name === def.name),
    ),
    ...run.steps
      .filter(
        (step) =>
          !steps.some(([name]) => name === step.name) &&
          !workflow?.definition.steps.some((def) => def.name === step.name),
      )
      .map((step) => ({ name: step.name, title: step.name })),
  ];

  function toggle(name: string) {
    setExpanded((items) =>
      items.includes(name)
        ? items.filter((item) => item !== name)
        : [...items, name],
    );
  }

  return (
    <section className="ruled-section">
      <h2>Workflow steps</h2>
      <p className="help">
        Expand a step for its exact records, evidence and model calls.
      </p>
      <div
        className="table-scroll"
        role="region"
        aria-label="Workflow steps"
        tabIndex={0}
      >
        <table className="step-table">
          <thead>
            <tr>
              {["Step", "Status / reason", "Duration", "Model calls", "Model"].map(
                (label) => (
                  <th scope="col" key={label}>
                    {label}
                  </th>
                ),
              )}
            </tr>
          </thead>
          <tbody>
            {definitions.map((definition, index) => {
              const step = run.steps.find(
                (item) => item.name === definition.name,
              );
              const stage = run.output?.stages.find(
                (item) => item.stage === definition.name,
              );
              const output =
                step?.output && typeof step.output === "object"
                  ? (step.output as Record<string, unknown>)
                  : null;
              const explanation =
                stage?.explanation ||
                (typeof output?.explanation === "string"
                  ? output.explanation
                  : "");
              const reason =
                explanation ||
                (typeof output?.reason === "string" ? output.reason : "");
              const details = output?.details as Record<string, unknown> | undefined;
              const stopped = run.status === "cancelled" &&
                step?.status === "failed" && details?.error_type === "CancelledError";
              const storyStatus = run.summary?.steps.find(
                (item) => item.name === definition.name,
              )?.status;
              const status = storyStatus ?? (stopped ? "stopped" : step?.status) ??
                (run.status === "cancelled" ? "pending" : "waiting");
              const open = expanded.includes(definition.name);
              return (
                <Fragment key={definition.name}>
                  <tr>
                    <th scope="row">
                      <button
                        className="step-toggle"
                        type="button"
                        aria-label={definition.title}
                        aria-expanded={open}
                        aria-controls={`step-${definition.name}`}
                        disabled={!step}
                        onClick={() => toggle(definition.name)}
                      >
                        <span className="step-chevron">
                          <Icon name="arrow" size={16} />
                        </span>
                        <span className="mono help" aria-hidden="true">
                          {String(index + 1).padStart(2, "0")}
                        </span>
                        {definition.title}
                      </button>
                    </th>
                    <td>
                      <StatusChip status={status} />
                      {step?.status === "skipped" && (
                        <p className="step-reason">
                          {reason || "No skip reason was recorded."}
                        </p>
                      )}
                    </td>
                    <td className="mono">
                      {duration(step?.duration_ms ?? null)}
                    </td>
                    <td className="mono">{step?.model_calls.length ?? 0}</td>
                    <td className="mono model-cell">
                      {step?.model_calls.length
                        ? step.model?.trim() || "Not recorded"
                        : "No model call"}
                    </td>
                  </tr>
                  <tr id={`step-${definition.name}`} hidden={!open}>
                    <td colSpan={5}>
                      {step && (
                        <div className="step-records">
                          {explanation && <p>{explanation}</p>}
                          <div className="step-json">
                            <Json value={step.input} label="Step input" />
                            <ModelCalls step={step} promptsOnly />
                          </div>
                          <Json value={step.output} label="Exact output JSON" />
                          <details>
                            <summary>Evidence ({step.evidence?.length ?? 0})</summary>
                            <Evidence step={step} />
                          </details>
                          <details>
                            <summary>Model calls ({step.model_calls.length})</summary>
                            <ModelCalls step={step} />
                          </details>
                        </div>
                      )}
                    </td>
                  </tr>
                </Fragment>
              );
            })}
          </tbody>
        </table>
      </div>
    </section>
  );
}
