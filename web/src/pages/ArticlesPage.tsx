import { useLayoutEffect, type MouseEvent } from "react";
import { useApi, useArticles, useArticleInventory } from "../api/hooks";
import { Link, navigate, useRoute, useScrollRestoration } from "../router";
import { VERDICTS, type ArticleStatus, type ArticleSummary } from "../types";
import { articleDate } from "../format";
import { VerdictChip, verdictLabel } from "../components/VerdictChip";
import {
  ArticleStatusChip, articleStatuses,
} from "../components/ArticleStatusChip";
import { EmptyState } from "../components/EmptyState";
import { ErrorState } from "../components/ErrorState";
import { Loading } from "../components/Loading";
import { ArchiveImport, archiveImportAvailable } from "../components/ArchiveImport";

function ArticleRow({ article, back }: { article: ArticleSummary; back: string }) {
  const href = `/articles/${encodeURIComponent(article.ref)}`;
  const state = { articlesBack: back };
  function openRow(event: MouseEvent<HTMLTableRowElement>) {
    if (
      event.defaultPrevented || event.button !== 0 || event.metaKey ||
      event.ctrlKey || event.shiftKey || event.altKey ||
      (event.target instanceof Element && event.target.closest("a, button"))
    ) return;
    navigate(href, { state });
  }
  return (
    <tr onClick={openRow}>
      <td className="article-cell">
        <Link href={href} state={state}>{article.title || "Untitled article"}</Link>
        <div className="article-meta">{article.source || article.host}</div>
      </td>
      <td>{articleDate(article.published_at)}</td>
      <td>{article.removed_at === null && !article.removal_checked
        ? <span className="help">Not checked yet</span>
        : articleDate(article.removed_at)}</td>
      <td><ArticleStatusChip status={article.status} /></td>
      <td>
        {article.latest_verdict && <VerdictChip verdict={article.latest_verdict} />}
      </td>
      <td>{article.run_count}</td>
      <td>{articleDate(article.latest_run_at)}</td>
    </tr>
  );
}

export function ArticlesPage() {
  const route = useRoute();
  const { token, epoch, status: service, articlesLedgerEpoch: ledgerEpoch } = useApi();
  const canImport = archiveImportAvailable(service);
  const epochChanged =
    ledgerEpoch.current.token !== token || ledgerEpoch.current.epoch !== epoch;
  const { query } = route;
  const q = (query.get("q") ?? "").slice(0, 200);
  const requestedStatus = query.get("status") ?? "";
  const status = Object.hasOwn(articleStatuses, requestedStatus)
    ? requestedStatus as ArticleStatus : "";
  const verdict = VERDICTS.includes(query.get("verdict") ?? "")
    ? query.get("verdict")! : "";
  const category = ["live", "benchmark", "all"].includes(query.get("category") ?? "")
    ? query.get("category")! : "all";
  const parsedOffset = Number(query.get("offset") ?? 0);
  const offset =
    !epochChanged && Number.isSafeInteger(parsedOffset) && parsedOffset > 0
      ? Math.floor(parsedOffset / 50) * 50 : 0;
  const server = new URLSearchParams({ limit: "50", offset: String(offset) });
  if (q) server.set("q", q);
  if (status) server.set("status", status);
  if (verdict) server.set("verdict", verdict);
  if (category !== "all") server.set("category", category);
  const { data, loading, refreshing, error, refresh, fresh } = useArticles(
    server.toString(),
  );
  const inventory = useArticleInventory(data?.total === 0);
  const total = data?.total ?? 0;
  const articles = data?.items ?? [];
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
  }, [correctingPage, epochChanged, token, epoch, ledgerEpoch, total,
    route.pathname, route.search]);
  const waitingForInventory = total === 0 && inventory.loading;
  useScrollRestoration(
    !correctingPage && !waitingForInventory && (!!data || !!error),
  );

  function update(name: string, value: string, filter = true) {
    const next = new URLSearchParams(route.search);
    if (value) next.set(name, value);
    else next.delete(name);
    if (filter) next.delete("offset");
    navigate(`${route.pathname}${next.size ? `?${next}` : ""}`, { replace: filter });
  }
  return (
    <section aria-labelledby="articles-title">
      <div className="page-heading ledger-heading">
        <div className="ledger-title">
          <p className="eyebrow">Newsroom ledger</p>
          <h1 id="articles-title">Articles</h1>
        </div>
        <div className="actions ledger-refresh">
          {refreshing && <span className="help" role="status">Refreshing…</span>}
          <button className="button secondary" onClick={refresh}>Refresh</button>
        </div>
        <p className="help ledger-description">
          Open an article to read what the audit did and the evidence behind it.
        </p>
        {canImport && (
          <div className="ledger-create">
            <ArchiveImport onImported={refresh} />
          </div>
        )}
      </div>
      <div className="filters" role="search" aria-label="Filter articles">
        <label>
          Search
          <input type="search" maxLength={200} value={q}
            placeholder="Title, outlet or URL"
            onChange={(e) => update("q", e.target.value)} />
        </label>
        <label>
          Status
          <select value={status} onChange={(e) => update("status", e.target.value)}>
            <option value="">All statuses</option>
            {Object.entries(articleStatuses).map(([value, label]) => (
              <option key={value} value={value}>{label}</option>
            ))}
          </select>
        </label>
        <label>
          Verdict
          <select value={verdict} onChange={(e) => update("verdict", e.target.value)}>
            <option value="">All verdicts</option>
            {VERDICTS.map((v) => <option key={v} value={v}>{verdictLabel(v)}</option>)}
          </select>
        </label>
        <label>
          Category
          <select value={category}
            onChange={(e) => update("category", e.target.value)}>
            <option value="all">All articles</option>
            <option value="live">Live articles</option>
            <option value="benchmark">Benchmark cases</option>
          </select>
        </label>
      </div>
      {error && <ErrorState message={error} onRetry={refresh} />}
      {loading || correctingPage ? <Loading>Loading articles…</Loading> : data && (
        articles.length ? (
          <div className="table-scroll" role="region"
            aria-label="Article ledger" tabIndex={0}>
            <table className="ledger articles-ledger">
              <thead><tr>
                {["Article / outlet", "Published", "Removed", "Audit status",
                  "Latest verdict", "Runs", "Last run"].map((label) => (
                  <th scope="col" key={label}>{label}</th>
                ))}
              </tr></thead>
              <tbody>{articles.map((article) => (
                <ArticleRow key={article.ref} article={article}
                  back={`${route.pathname}${route.search}`} />
              ))}</tbody>
            </table>
          </div>
        ) : inventory.loading ? <Loading>Checking article inventory…</Loading> : (
          <EmptyState title={inventory.data?.total === 0
            ? "No articles yet" : "No articles match these filters"}>
            <p>{inventory.data?.total !== 0
              ? "Try another search, status, verdict or category."
              : canImport
                ? "Use Import from archive above to load recently removed articles, " +
                  "or submit one for audit."
                : service?.mode === "live"
                  ? "Archive reads are off on this server, so articles appear only " +
                    "when submitted for audit."
                  : "Articles appear here when imported or submitted for audit."}</p>
            {inventory.data?.total !== 0 && (
              <Link href="/articles">Clear filters</Link>
            )}
          </EmptyState>
        )
      )}
      {data && !correctingPage && (
        <div className="pagination">
          <button className="button secondary" disabled={offset === 0}
            onClick={() => update("offset", String(Math.max(0, offset - 50)), false)}>
            Previous
          </button>
          <p aria-live="polite">
            {total && articles.length && offset < total
              ? `${offset + 1}–${Math.min(offset + articles.length, total)}` : "0"}
            {" "}of {total} articles
          </p>
          <button className="button secondary" disabled={offset + 50 >= total}
            onClick={() => update("offset", String(offset + 50), false)}>
            Next
          </button>
        </div>
      )}
    </section>
  );
}
