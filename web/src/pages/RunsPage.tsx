import { useLayoutEffect, type MouseEvent } from "react";
import { useApi, useQueue, useRuns } from "../api/hooks";
import { Link, navigate, useRoute, useScrollRestoration } from "../router";
import { VERDICTS, type RunSummary } from "../types";
import {
  ARTICLE_ORIGIN_LABEL,
  POLL_ORIGIN_LABEL,
  date,
  ledgerLabel,
  originLabel,
  pending,
  runDuration,
} from "../format";
import { VerdictChip, verdictLabel } from "../components/VerdictChip";
import { StatusChip } from "../components/StatusChip";
import { EmptyState } from "../components/EmptyState";
import { ErrorState } from "../components/ErrorState";
import { StopRunButton } from "../components/StopRunButton";
import { QueueBar } from "../components/QueueBar";
import { Loading } from "../components/Loading";

const statuses = ["queued", "running", "succeeded", "failed", "cancelled"];
const columns = [
  "Article",
  "Origin",
  "Verdict",
  "Status",
  "Started",
  "Duration",
  "Reviews",
];

function RunRow({ run, back, onStopped }: {
  run: RunSummary;
  back: string;
  onStopped: () => void;
}) {
  const href = `/runs/${encodeURIComponent(run.id)}`;
  const state = { runsBack: back };
  const label = ledgerLabel(run);

  function openRow(event: MouseEvent<HTMLTableRowElement>) {
    if (
      event.defaultPrevented ||
      event.button !== 0 ||
      event.metaKey ||
      event.ctrlKey ||
      event.shiftKey ||
      event.altKey ||
      (event.target instanceof Element && event.target.closest("a, button"))
    ) {
      return;
    }
    navigate(href, { state });
  }

  return (
    <tr onClick={openRow}>
      <td className="article-cell">
        <Link href={href} state={state}>
          {run.article_title || "Untitled article"}
        </Link>
        <div className="article-meta">
          <span className="mono">{run.id}</span>
          {label && <> · {label}</>}
        </div>
      </td>
      <td>{originLabel(run)}</td>
      <td>
        {run.status === "cancelled" ? "—" : <VerdictChip verdict={run.verdict} />}
      </td>
      <td>
        <div className="run-row-controls">
          <StatusChip status={run.status} />
          <StopRunButton run={run} variant="compact" onStopped={onStopped} />
        </div>
      </td>
      <td>
        <time dateTime={run.created_at}>{date(run.created_at)}</time>
      </td>
      <td className={pending(run.status) ? undefined : "mono"}>
        {runDuration(run)}
      </td>
      <td>{run.reactions.length}</td>
    </tr>
  );
}

export function RunsPage({
  benchmark = false,
  onCreate,
}: {
  benchmark?: boolean;
  onCreate?: () => void;
}) {
  const route = useRoute();
  const { token, epoch, ledgerEpoch } = useApi();
  const epochChanged =
    ledgerEpoch.current.token !== token || ledgerEpoch.current.epoch !== epoch;
  const { query } = route;
  const requestedOrigin = query.get("origin") ?? "";
  const origin = benchmark
    ? "benchmark"
    : ["benchmark", "live", "api", "webhook", "article", "poll"].includes(requestedOrigin)
      ? requestedOrigin
      : "";
  const verdict = VERDICTS.includes(query.get("verdict") ?? "")
    ? query.get("verdict")!
    : "";
  const status = statuses.includes(query.get("status") ?? "")
    ? query.get("status")!
    : "";
  const workflow = query.get("workflow_id") ?? "";
  const filtered = !!((!benchmark && origin) || verdict || status || workflow);
  const parsedOffset = Number(query.get("offset") ?? 0);
  const offset =
    !epochChanged && Number.isSafeInteger(parsedOffset) && parsedOffset > 0
      ? Math.floor(parsedOffset / 50) * 50
      : 0;
  const server = new URLSearchParams({ limit: "50", offset: String(offset) });
  if (origin === "benchmark" || origin === "live") {
    server.set("category", origin);
  } else if (origin) {
    server.set("origin", origin);
  }
  if (status) server.set("status", status);
  if (verdict) server.set("verdict", verdict);
  if (workflow) server.set("workflow_id", workflow);

  const { data, loading, refreshing, error, refresh, fresh } = useRuns(
    server.toString(),
  );
  const queue = useQueue();
  const runs = data?.items ?? [];
  const total = data?.total ?? 0;
  const back = `${route.pathname}${route.search}`;
  const invalidOffset =
    fresh && !!data && query.has("offset") && (total === 0 || offset >= total);
  const correctingPage = (epochChanged && query.has("offset")) || invalidOffset;

  useLayoutEffect(() => {
    ledgerEpoch.current = { token, epoch };
    if (!correctingPage) return;
    const next = new URLSearchParams(route.search);
    const lastOffset =
      epochChanged || total === 0 ? 0 : Math.floor((total - 1) / 50) * 50;
    if (lastOffset) next.set("offset", String(lastOffset));
    else next.delete("offset");
    navigate(`${route.pathname}${next.size ? `?${next}` : ""}`, { replace: true });
  }, [
    correctingPage,
    epochChanged,
    token,
    epoch,
    ledgerEpoch,
    total,
    route.pathname,
    route.search,
  ]);

  useScrollRestoration(!correctingPage && (!!data || !!error));

  function update(name: string, value: string, filter = true) {
    const next = new URLSearchParams(route.search);
    if (value) next.set(name, value);
    else next.delete(name);
    if (filter) next.delete("offset");
    navigate(`${route.pathname}${next.size ? `?${next}` : ""}`, { replace: filter });
  }

  return (
    <section aria-labelledby="runs-title">
      <div className="page-heading ledger-heading">
        <div className="ledger-title">
          <p className="eyebrow">Audit ledger</p>
          <h1 id="runs-title">{benchmark ? "Benchmarks" : "Runs"}</h1>
        </div>
        <div className="actions ledger-refresh">
          {refreshing && (
            <span className="help" role="status">
              Refreshing…
            </span>
          )}
          <button
            className="button secondary"
            onClick={() => {
              refresh();
              queue.refresh();
            }}
          >
            Refresh
          </button>
        </div>
        <p className="help ledger-description">
          {benchmark
            ? "Controlled cases and their saved evidence."
            : "Benchmark and live audits, with their evidence and human reviews."}
        </p>
        {onCreate && (
          <button className="button primary ledger-create" onClick={onCreate}>
            New benchmark run
          </button>
        )}
      </div>
      {!benchmark && (
        <QueueBar key={epoch} queue={queue} onChanged={refresh} />
      )}
      <div className="filters" role="search" aria-label="Filter runs">
        {!benchmark && (
          <label>
            Origin
            <select
              value={origin}
              onChange={(e) => update("origin", e.target.value)}
            >
              <option value="">All</option>
              <option value="benchmark">Benchmark</option>
              <option value="live">Live (all)</option>
              <option value="api">Live · API</option>
              <option value="webhook">Live · Webhook</option>
              <option value="article">{ARTICLE_ORIGIN_LABEL}</option>
              <option value="poll">{POLL_ORIGIN_LABEL}</option>
            </select>
          </label>
        )}
        <label>
          Verdict
          <select
            value={verdict}
            onChange={(e) => update("verdict", e.target.value)}
          >
            <option value="">All verdicts</option>
            {VERDICTS.map((v) => (
              <option key={v} value={v}>
                {verdictLabel(v)}
              </option>
            ))}
          </select>
        </label>
        <label>
          Status
          <select
            value={status}
            onChange={(e) => update("status", e.target.value)}
          >
            <option value="">All statuses</option>
            {statuses.map((s) => (
              <option key={s} value={s}>
                {s[0].toUpperCase() + s.slice(1)}
              </option>
            ))}
          </select>
        </label>
        <label className={benchmark ? "workflow-filter" : undefined}>
          Workflow version
          <input
            value={workflow}
            onChange={(e) => update("workflow_id", e.target.value)}
            placeholder="All workflow versions"
          />
        </label>
      </div>
      {error && <ErrorState message={error} onRetry={refresh} />}
      {loading || correctingPage ? (
        <Loading>Loading runs…</Loading>
      ) : data && (
        !runs.length ? (
          <EmptyState title={filtered
            ? "No runs match these filters" : "No runs yet"}>
            <p>{filtered
              ? "Adjust the filters, or create a case from Benchmarks."
              : benchmark
                ? "Choose New benchmark run to submit a controlled case."
                : "Create a case from Benchmarks."}</p>
            {!benchmark && <Link href="/benchmarks">Open Benchmarks</Link>}
          </EmptyState>
        ) : (
          <div
            className="table-scroll"
            role="region"
            aria-label="Run ledger"
            tabIndex={0}
          >
            <table className="ledger">
              <thead>
                <tr>
                  {columns.map((h) => (
                    <th scope="col" key={h}>
                      {h}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {runs.map((run) => (
                  <RunRow
                    key={run.id} run={run} back={back} onStopped={refresh}
                  />
                ))}
              </tbody>
            </table>
          </div>
        )
      )}
      {data && !correctingPage && (
        <div className="pagination">
          <button
            className="button secondary"
            disabled={offset === 0}
            onClick={() =>
              update("offset", String(Math.max(0, offset - 50)), false)
            }
          >
            Previous
          </button>
          <p aria-live="polite">
            {total && runs.length && offset < total
              ? `${offset + 1}–${Math.min(offset + runs.length, total)}`
              : "0"}{" "}
            of {total} runs
          </p>
          <button
            className="button secondary"
            disabled={offset + 50 >= total}
            onClick={() => update("offset", String(offset + 50), false)}
          >
            Next
          </button>
        </div>
      )}
    </section>
  );
}
