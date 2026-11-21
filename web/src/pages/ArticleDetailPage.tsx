import { useEffect, useRef, useState } from "react";
import { useApi, useArticle, useRun } from "../api/hooks";
import { ApiError, auditArticle, articleAuditError } from "../api/client";
import { Link, useScrollRestoration } from "../router";
import { articleDate, humanize, originLabel, pending, shortWorkflow } from "../format";
import { VerdictChip } from "../components/VerdictChip";
import { StatusChip } from "../components/StatusChip";
import { StopRunButton } from "../components/StopRunButton";
import { FactsGrid } from "../components/FactsGrid";
import { ErrorState } from "../components/ErrorState";
import { Loading } from "../components/Loading";
import { EmptyState } from "../components/EmptyState";
import { Review } from "../features/review/Review";
import { AuditStory } from "../features/steps/AuditStory";
import { ArticleText } from "../features/articles/ArticleText";
import { externalUrl, originalIsDead } from "../features/steps/evidenceSummary";
import type { ArticleRun } from "../types";

export function ArticleDetailPage({ articleRef }: { articleRef: string }) {
  const { token, epoch, status: service, articlesCache } = useApi();
  const resource = useArticle(articleRef);
  const { data: article, loading, refreshing, error, refresh, setData } = resource;
  const latest = article?.runs[0];
  const detail = useRun(latest?.id ?? null);
  const run = detail.data?.id === latest?.id ? detail.data : null;
  const summary = run?.summary ?? latest?.summary;
  const running = !!latest && pending(run?.status ?? latest.status);
  const recorded = run?.steps.length ?? summary?.steps.filter(
    (step) => !["pending", "running"].includes(step.status),
  ).length ?? 0;
  const progress = Math.min(recorded + 1, 6);
  const done = Math.min(recorded, 6);
  const [submitting, setSubmitting] = useState(false);
  const [actionError, setActionError] = useState("");
  const controller = useRef<AbortController | null>(null);
  useEffect(() => {
    setSubmitting(false);
    setActionError("");
    return () => controller.current?.abort();
  }, [articleRef, token, epoch]);
  // When the latest run leaves queued/running (stopped here, on another page, or finished),
  // the cached article list and this article are stale. Reacting to the transition also covers a
  // stop whose request was aborted because the poll saw the cancellation first.
  const wasRunning = useRef<{ id: string; running: boolean } | null>(null);
  useEffect(() => {
    const previous = wasRunning.current;
    wasRunning.current = latest ? { id: latest.id, running } : null;
    if (previous?.running && latest && previous.id === latest.id && !running) {
      articlesCache.clear();
      refresh();
    }
  }, [latest?.id, running]);
  useScrollRestoration(!!article || !!error);
  const backState: unknown = window.history.state?.articlesBack;
  const back = typeof backState === "string" && /^\/articles(\?|$)/.test(backState)
    ? backState : "/articles";

  async function startAudit() {
    if (!article || submitting || running) return;
    controller.current?.abort();
    const current = new AbortController();
    controller.current = current;
    setSubmitting(true);
    setActionError("");
    try {
      const result = await auditArticle(articleRef, token, current.signal);
      if (current.signal.aborted) return;
      const history: ArticleRun = {
        id: result.id, status: result.status,
        verdict: result.verdict, created_at: result.created_at,
        finished_at: result.finished_at, origin: result.origin,
        category: result.category, workflow_id: result.workflow_id,
        label: result.label, summary: result.summary ?? null,
      };
      setData({
        ...article, latest_run_id: result.id, latest_run_status: history.status,
        latest_verdict: history.verdict, latest_run_at: history.created_at,
        status: pending(result.status) ? "in_progress"
          : result.status === "failed" || result.status === "cancelled"
            ? result.status : "audited",
        run_count: article.run_count + 1, runs: [history, ...article.runs],
      });
      articlesCache.clear();
      refresh();
    } catch (error) {
      if (current.signal.aborted) return;
      setActionError(articleAuditError(error, article.category === "live" &&
        service?.mode === "live" && !service.live_ingestion_enabled));
      if (error instanceof ApiError && error.status === 409) refresh();
    } finally {
      if (!current.signal.aborted) setSubmitting(false);
    }
  }

  const source = run?.steps.find((step) => step.name === "source");
  const original = article?.links.original_url ?? "";
  const originalHref = externalUrl(original);
  return (
    <>
      <Link href={back} className="back-link">← All articles</Link>
      {error && <ErrorState message={error} onRetry={refresh} />}
      {loading ? <Loading>Loading article…</Loading> : article && (
        <div className="article-layout">
          <article className="article-main">
            <header className="page-heading">
              <div>
                <p className="eyebrow">Case file · {article.ref}</p>
                <h1>{article.title || "Untitled article"}</h1>
                <p className="help article-dates">
                  <strong>{article.source || article.host}</strong>
                  <span>Published {articleDate(article.published_at)}</span>
                  <span>Removed {articleDate(article.removed_at)}</span>
                </p>
              </div>
              <div className="actions">
                {refreshing && <span className="help" role="status">Refreshing…</span>}
                <button className="button secondary" onClick={() => {
                  refresh(); detail.refresh();
                }}>Refresh</button>
              </div>
            </header>
            {!latest ? (
              <EmptyState title="No audit yet.">
                <p>Run the first audit to see what happened.</p>
              </EmptyState>
            ) : (
              <section className="verdict-overview" aria-label="Verdict">
                {running ? (
                  <>
                    <h2 className="verdict-headline">Audit in progress</h2>
                    <p role="status">Step {progress} of 6 · {humanize(latest.status)}</p>
                    <progress aria-label="Audit progress" max={6} value={done} />
                  </>
                ) : summary ? (
                  <>
                    {latest.status === "cancelled"
                      ? <span className="chip status-chip">
                          {summary.verdict_label}
                        </span>
                      : <VerdictChip verdict={summary.verdict} />}
                    <h2 className="verdict-headline">{summary.headline}</h2>
                    {summary.sentences.map((sentence, index) => (
                      <p className="findings" key={index}>{sentence}</p>
                    ))}
                    <p className="verdict-note">
                      {summary.human_review_note ||
                        "This is not a finding of censorship; a person decides."}
                    </p>
                  </>
                ) : <p>No summary was recorded for this run.</p>}
                <Link href={`/runs/${encodeURIComponent(latest.id)}`}>
                  Open full run
                </Link>
              </section>
            )}
            {detail.error && <ErrorState message={detail.error}
              onRetry={detail.refresh} />}
            {summary && <AuditStory summary={summary} run={run} />}
            <ArticleText key={`${articleRef}:${epoch}`} article={article} />
          </article>
          <aside className="article-rail" aria-label="Article facts and actions">
            <h2>Article facts</h2>
            <FactsGrid facts={[
              { label: "Reference", value: <code>{article.ref}</code> },
              ...(article.archive_id ? [{ label: "Archive id",
                value: <code>{article.archive_id}</code> }] : []),
              { label: "Category", value: humanize(article.category) },
              { label: "Original URL", value: <>
                <span>{original}</span>
                {originalHref && <> · <a href={originalHref} target="_blank"
                  rel="noopener noreferrer">Open original ↗</a></>}
                {originalIsDead(source, original) && (
                  <p className="help">No longer available</p>
                )}
              </> },
            ]} />
            <button className="button primary article-audit-button"
              disabled={running || submitting} onClick={startAudit}>
              {running ? `Audit running · step ${progress} of 6`
                : submitting ? "Starting audit…"
                  : latest ? "Re-run audit" : "Run first audit"}
            </button>
            {running && latest && (
              <StopRunButton
                key={`${epoch}:${latest.id}`}
                run={{ id: latest.id, status: run?.status ?? latest.status }}
                onStopped={(stopped) => {
                  // Show the stopped run at once, then reload the article and its list entry.
                  detail.setData(stopped);
                  articlesCache.clear();
                  refresh();
                }}
              />
            )}
            <p className="help">{latest
              ? "Starts a new run; earlier runs stay in history."
              : "Runs the audit with the current workflow."}</p>
            {actionError && <ErrorState message={actionError} />}
            {run ? <Review key={`${run.id}:${epoch}`} run={run}
              token={token} onSaved={detail.setData} /> : latest && detail.loading
              ? <Loading>Loading review…</Loading> : (
                <section className="ruled-section">
                  <h2>Human review</h2>
                  <p className="help">{latest
                    ? "Review loads with the latest run’s saved records."
                    : "Available after the first audit."}</p>
                </section>
              )}
            <section className="ruled-section" aria-labelledby="article-history-title">
              <h2 id="article-history-title">Run history · {article.run_count}</h2>
              {article.runs.length ? <ul className="article-history">
                {article.runs.map((item) => (
                  <li key={item.id}>
                    <Link href={`/runs/${encodeURIComponent(item.id)}`}>
                      {articleDate(item.created_at)}
                      <span className="sr-only"> · {item.id}</span>
                    </Link>
                    <div className="actions">
                      {item.verdict && <VerdictChip verdict={item.verdict} />}
                      <StatusChip status={item.status} />
                    </div>
                    <p className="help">{originLabel(item)} · {humanize(item.category)}</p>
                    <code title={item.workflow_id}>{shortWorkflow(item.workflow_id)}</code>
                  </li>
                ))}
              </ul> : <p className="help">No saved runs yet.</p>}
            </section>
          </aside>
        </div>
      )}
    </>
  );
}
