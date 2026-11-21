import { useState } from "react";
import type { Workflow } from "../../types";
import { ErrorState as ErrorBox } from "../../components/ErrorState";
import { Icon } from "../../components/Icon";
import { Json } from "../../components/Json";
import { date } from "../../format";

export function WorkflowView({
  workflow,
  error,
  onRetry,
  embedded = false,
}: {
  workflow: Workflow | null;
  error?: string;
  onRetry?: () => void;
  embedded?: boolean;
}) {
  const [source, setSource] = useState("");
  const failure = error ? (
    <div className="snapshot-unavailable">
      <ErrorBox
        message={
          workflow
            ? `Could not refresh workflow snapshot. ${error}`
            : `Workflow snapshot unavailable. ${error}`
        }
      />
      {onRetry && (
        <button className="button secondary small" onClick={onRetry}>
          <Icon name="refresh" size={14} /> Retry snapshot
        </button>
      )}
    </div>
  ) : null;
  if (!workflow)
    return (
      failure ?? <div className="loading">Loading workflow snapshot…</div>
    );
  const sources = {
    ...workflow.definition.sources,
    ...Object.fromEntries(
      Object.entries(workflow.definition.dependencies ?? {}).map(
        ([name, content]) => [`dependencies/${name}`, content],
      ),
    ),
  };
  const activeSource = source in sources ? source : Object.keys(sources)[0];
  return (
    <div className={`workflow-view ${embedded ? "embedded" : ""}`}>
      {failure}
      <div className="workflow-version">
        <span className="workflow-symbol">
          <Icon name="flow" size={23} />
        </span>
        <div>
          <span className="eyebrow">IMMUTABLE WORKFLOW SNAPSHOT</span>
          <code>{workflow.id}</code>
          <p>
            Captured {date(workflow.created_at)} ·{" "}
            {workflow.definition.steps.length} steps
          </p>
        </div>
      </div>
      <div className="workflow-definition">
        {workflow.definition.steps.map((step, index) => (
          <div className="definition-step" key={step.name}>
            <span>{String(index + 1).padStart(2, "0")}</span>
            <div>
              <h3>{step.title}</h3>
              <p>{step.description}</p>
              <code>{step.name}</code>
            </div>
            {index < workflow.definition.steps.length - 1 && (
              <Icon name="arrow" size={16} />
            )}
          </div>
        ))}
      </div>
      <details className="settings-details">
        <summary>Workflow settings & edges</summary>
        <Json
          value={{
            settings: workflow.definition.settings,
            edges: workflow.definition.edges,
            decision: workflow.definition.decision,
            format_version: workflow.definition.format_version,
          }}
          label="Snapshot configuration"
        />
      </details>
      <div className="subsection-title">
        <h3>Source snapshot</h3>
        <span className="help">As executed for this version</span>
      </div>
      {Object.keys(sources).length ? (
        <>
          <label className="source-select">
            <span className="sr-only">Source file</span>
            <select
              value={activeSource}
              onChange={(e) => setSource(e.target.value)}
            >
              {Object.keys(sources).map((name) => (
                <option key={name} value={name}>
                  {name}
                </option>
              ))}
            </select>
          </label>
          <div className="source-view">
            <div className="inspector-bar">
              <span>{activeSource}</span>
              <Icon name="code" size={14} />
            </div>
            <pre tabIndex={0}>
              {typeof sources[activeSource] === "string"
                ? (sources[activeSource] as string)
                : JSON.stringify(sources[activeSource], null, 2)}
            </pre>
          </div>
        </>
      ) : (
        <p className="help">No source files are included in this snapshot.</p>
      )}
    </div>
  );
}
