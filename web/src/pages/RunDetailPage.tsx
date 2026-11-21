import { useEffect, useRef, useState } from "react";
import { useApi, useRun, useWorkflow } from "../api/hooks";
import { request, errorMessage } from "../api/client";
import { Link, useScrollRestoration } from "../router";
import {
  date,
  humanize,
  originLabel,
  pending,
  runDuration,
  runModel,
} from "../format";
import { VerdictChip, verdictLabel } from "../components/VerdictChip";
import { StopRunButton } from "../components/StopRunButton";
import { StatusChip } from "../components/StatusChip";
import { FactsGrid } from "../components/FactsGrid";
import { ErrorState } from "../components/ErrorState";
import { Loading } from "../components/Loading";
import { Json } from "../components/Json";
import { Badge } from "../components/Badge";
import { StepTable } from "../features/steps/StepTable";
import { WorkflowView } from "../features/workflow/WorkflowView";
import { Review } from "../features/review/Review";
import type { Run } from "../types";

export function RunDetailPage({ id }: { id: string }) {
  const { token, epoch } = useApi();
  const {
    data: run,
    loading,
    refreshing,
    error,
    refresh,
    setData,
  } = useRun(id);
  useScrollRestoration(!!run || !!error);
  const snapshot = useWorkflow(run?.workflow_id ?? null);
  const current = useWorkflow();
  const workflow =
    snapshot.data?.id === run?.workflow_id
      ? snapshot.data
      : current.data?.id === run?.workflow_id
        ? current.data
        : null;
  const backState: unknown = window.history.state?.runsBack;
  const back =
    typeof backState === "string" && /^\/(runs|benchmarks)(\?|$)/.test(backState)
      ? backState
      : "/runs";
  const [exportError, setExportError] = useState("");
  const [exporting, setExporting] = useState(false);
  const exportController = useRef<AbortController | null>(null);
  useEffect(() => () => exportController.current?.abort(), []);

  async function exportRun() {
    setExporting(true);
    setExportError("");
    const controller = new AbortController();
    exportController.current = controller;
    try {
      const data = await request<Run>(
        `/api/runs/${encodeURIComponent(id)}/export`,
        token,
        { signal: controller.signal },
      );
      if (controller.signal.aborted) return;
      const url = URL.createObjectURL(
        new Blob([JSON.stringify(data, null, 2)], {
          type: "application/json",
        }),
      );
      const link = document.createElement("a");
      link.href = url;
      link.download = `audit-${id}.json`;
      link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (error) {
      if (!controller.signal.aborted) setExportError(errorMessage(error));
    } finally {
      if (!controller.signal.aborted) setExporting(false);
    }
  }

  return (
    <>
      <Link href={back} className="back-link">
        ← Back to {back.startsWith("/benchmarks") ? "Benchmarks" : "Runs"}
      </Link>
      {error && <ErrorState message={error} onRetry={refresh} />}
      {loading ? (
        <Loading>Loading run and evidence…</Loading>
      ) : run && (
        <article className="run-detail">
          <header className="page-heading">
            <div>
              <p className="eyebrow">Saved audit run</p>
              <h1>
                {run.article_ref ? (
                  <Link href={`/articles/${encodeURIComponent(run.article_ref)}`}>
                    {run.article_title || "Untitled article"}
                  </Link>
                ) : run.article_title || "Untitled article"}
              </h1>
              <p className="mono help">{run.id}</p>
            </div>
            <div className="actions">
              {refreshing && (
                <span className="help" role="status">
                  Refreshing…
                </span>
              )}
              {pending(run.status) && (
                <StopRunButton
                  key={`${epoch}:${id}`}
                  run={run}
                  onStopped={refresh}
                />
              )}
              <button className="button secondary" onClick={refresh}>
                Refresh run
              </button>
              <button
                className="button primary"
                disabled={exporting}
                onClick={exportRun}
              >
                {exporting ? "Exporting…" : "Export JSON"}
              </button>
            </div>
          </header>
          {exportError && <ErrorState message={exportError} />}
          {run.error && run.status !== "cancelled" && (
            <ErrorState
              message={
                `${run.error.code}: ${run.error.message} ` +
                "Inspect the step records for details or create a new " +
                "benchmark to try again."
              }
            />
          )}
          <section className="verdict-overview" aria-label="Verdict">
            {run.status !== "cancelled" && <VerdictChip verdict={run.verdict} />}
            <h2 className="verdict-headline">
              {run.status === "cancelled"
                ? run.summary?.verdict_label ?? "Stopped by a person"
                : run.verdict
                  ? verdictLabel(run.verdict)
                  : pending(run.status)
                    ? "Audit in progress"
                    : "No verdict available"}
            </h2>
            {run.status !== "cancelled" && (
              <p className="verdict-note">
                This is not a finding of censorship; a person decides.
              </p>
            )}
            {run.status === "cancelled" ? (
              <p>
                {run.summary?.headline ??
                  "A person stopped this audit before it finished. This is not a result."}
              </p>
            ) : typeof run.output?.findings === "string" ? (
              <p className="findings">{run.output.findings}</p>
            ) : run.output?.findings != null ? (
              <details>
                <summary>Saved findings</summary>
                <Json value={run.output.findings} label="Findings" />
              </details>
            ) : (
              <p>
                {pending(run.status)
                  ? "The trace updates as each step completes."
                  : "No supporting explanation was recorded."}
              </p>
            )}
            {run.status !== "cancelled" && !!run.output?.flags.length && (
              <div className="flag-list">
                {run.output.flags.map((flag) => (
                  <Badge key={flag} tone="amber">
                    {humanize(flag)}
                  </Badge>
                ))}
              </div>
            )}
            {run.expected_verdict && run.status !== "cancelled" && (
              <p className="expected-comparison">
                Expected verdict: <VerdictChip verdict={run.expected_verdict} />
                {run.verdict && (
                  <span>
                    {run.verdict === run.expected_verdict
                      ? "Match"
                      : "Different prediction"}
                  </span>
                )}
              </p>
            )}
          </section>
          <FactsGrid
            facts={[
              { label: "Run id", value: <code>{run.id}</code> },
              {
                label: "Status",
                value: <StatusChip status={run.status} />,
              },
              {
                label: "Started",
                value: (
                  <time dateTime={run.created_at}>{date(run.created_at)}</time>
                ),
              },
              {
                label: "Finished",
                value: run.finished_at ? (
                  <time dateTime={run.finished_at}>{date(run.finished_at)}</time>
                ) : "—",
              },
              {
                label: "Duration",
                value: (
                  <span className={pending(run.status) ? undefined : "mono"}>
                    {runDuration(run)}
                  </span>
                ),
              },
              {
                label: "Workflow version",
                value: (
                  <Link
                    href={`/settings?workflow_id=${encodeURIComponent(run.workflow_id)}`}
                  >
                    <code>{run.workflow_id}</code>
                  </Link>
                ),
              },
              {
                label: "Model",
                value: <span className="mono">{runModel(run)}</span>,
              },
              { label: "Category", value: humanize(run.category) },
              { label: "Origin", value: originLabel(run) },
            ]}
          />
          <StepTable run={run} workflow={workflow} />
          <Review key={epoch} run={run} token={token} onSaved={setData} />
          <section className="ruled-section">
            <h2>Article and records</h2>
            <details>
              <summary>Submitted article</summary>
              <p className="article-url">{run.input.removed_article.url}</p>
              <div className="article-content" tabIndex={0}>
                {run.input.removed_article.content}
              </div>
            </details>
            <details>
              <summary>Exact submitted input</summary>
              <Json value={run.input} label="Run input" />
            </details>
            <details>
              <summary>Exact workflow output</summary>
              <Json value={run.output} label="Run output" />
            </details>
          </section>
          <section className="ruled-section">
            <h2>Saved workflow</h2>
            <details>
              <summary>Inspect immutable workflow snapshot</summary>
              <WorkflowView
                workflow={workflow}
                error={snapshot.error}
                onRetry={snapshot.refresh}
                embedded
              />
            </details>
          </section>
        </article>
      )}
    </>
  );
}
