import { useState } from "react";
import type { ArchiveState, PollingStatus, PollReason, SearchState } from "../types";
import { date } from "../format";
import { useApi, useServiceStatus, useWorkflow } from "../api/hooks";
import { useRoute, Link, useScrollRestoration } from "../router";
import { ConnectionDialog } from "../dialogs/ConnectionDialog";
import { FactsGrid } from "../components/FactsGrid";
import { ErrorState } from "../components/ErrorState";
import { Loading } from "../components/Loading";
import { WorkflowView } from "../features/workflow/WorkflowView";

const archiveLabels: Record<ArchiveState, string> = {
  disabled: "Disabled · classification catalog and related-removal evidence unavailable",
  unchecked: "Not checked yet",
  ok: "OK",
  wallet_missing: "Archive wallet not found",
  wallet_unreadable: "Archive wallet cannot be read",
  auth_unavailable: "Archive sign-in failed",
  unreachable: "Archive unreachable",
  rejected: "Archive refused the request",
};

const searchLabels: Record<SearchState, string> = {
  disabled: "Disabled",
  unchecked: "SearXNG · not checked yet",
  ok: "SearXNG · reachable",
  unreachable: "SearXNG · unreachable at the configured SEARXNG_URL",
  rejected: "SearXNG · refused the request (is JSON output enabled?)",
  degraded: "SearXNG · reachable, but its search engines are not answering (blocked or rate-limited)",
};

const pollReasons: Record<PollReason, string> = {
  ...archiveLabels,
  archive_error: "Archive read failed",
  archive_incomplete: "Archive listing incomplete",
  archive_page_failed: "Archive page read failed",
  archive_page_malformed: "Archive page malformed",
  archive_page_cap: "Archive page limit reached",
  poll_error: "Polling failed",
  import_in_progress: "Import in progress",
  queue_paused: "Queue paused",
};

function lastPoll(polling: PollingStatus) {
  const last = polling.last;
  if (!last) return "Not yet";
  const time = date(last.finished_at);
  const result = last.outcome === "skipped"
    ? `${time} · skipped: ${last.reason ? pollReasons[last.reason] : "Unknown"}`
    : `${time} · found ${last.found}, new ${last.new}, queued ${last.queued}`;
  return `${result} · unreadable ${last.unreadable}`;
}

export function SettingsPage() {
  const { token, setToken } = useApi();
  const { status, error, retry } = useServiceStatus();
  const { query } = useRoute();
  const version = query.get("workflow_id") || undefined;
  const workflow = useWorkflow(version);
  const [connectionOpen, setConnectionOpen] = useState(false);
  useScrollRestoration(
    !!error || !!workflow.error || (!!status && !!workflow.data),
  );

  return (
    <section>
      <div className="page-heading">
        <div>
          <p className="eyebrow">Workbench</p>
          <h1>Settings</h1>
          <p className="help">
            Connection, service capabilities and read-only workflow snapshots.
          </p>
        </div>
        <button
          className="button secondary"
          onClick={() => setConnectionOpen(true)}
        >
          Connection / API token
        </button>
      </div>
      {error && <ErrorState message={error} onRetry={retry} />}
      {!status && !error && <Loading>Loading service details…</Loading>}
      {status && (
        <FactsGrid
          facts={[
            {
              label: "Mode",
              value: status.mode === "test" ? "Test mode · demo data" : "Live",
            },
            {
              label: "Model",
              value: status.model_enabled ? status.model_name : "Disabled",
            },
            { label: "Provider", value: status.model_provider },
            { label: "Response format", value: status.response_format },
            { label: "Search", value: searchLabels[status.search] },
            {
              label: "Archive reads",
              value: archiveLabels[status.archive],
            },
            {
              label: "Polling",
              value: status.polling.enabled
                ? `Every ${status.polling.interval_minutes} min`
                : "Off",
            },
            {
              label: "Auto-audit",
              value: status.polling.auto_audit_enabled
                ? `On (up to ${status.polling.auto_audit_max_per_poll} per poll)`
                : "Off",
            },
            { label: "Sync lookback", value: `${status.polling.lookback_days} days` },
            { label: "Last poll", value: lastPoll(status.polling) },
            {
              label: "Prompts editable via API",
              value: status.prompts_editable
                ? "Yes · authenticated API only"
                : "No",
            },
            { label: "Archive writes", value: "Disabled" },
          ]}
        />
      )}
      {status && status.polling.history.length > 0 && (
        <section className="ruled-section" aria-labelledby="sync-history-title">
          <h2 id="sync-history-title">Sync history</h2>
          <div className="table-scroll" role="region"
            aria-label="Recent archive sync cycles" tabIndex={0}>
            <table className="sync-history">
              <thead><tr>
                <th scope="col">Time</th><th scope="col">Outcome</th>
                <th scope="col">Found</th><th scope="col">New</th>
                <th scope="col">Updated</th><th scope="col">Queued</th>
              </tr></thead>
              <tbody>{status.polling.history.map((cycle, index) => (
                <tr key={`${cycle.started_at}-${index}`}>
                  <td>{date(cycle.finished_at)}</td>
                  <td>{cycle.outcome === "ok" ? "OK" : "Skipped"}
                    {cycle.reason && ` · ${pollReasons[cycle.reason]}`}</td>
                  <td>{cycle.found}</td><td>{cycle.new}</td>
                  <td>{cycle.updated}</td><td>{cycle.queued}</td>
                </tr>
              ))}</tbody>
            </table>
          </div>
        </section>
      )}
      <section className="ruled-section">
        <div className="subsection-title">
          <h2>{version ? "Saved workflow" : "Current workflow"}</h2>
          {version && <Link href="/settings">View current workflow</Link>}
        </div>
        <WorkflowView
          workflow={workflow.data}
          error={workflow.error}
          onRetry={workflow.refresh}
        />
      </section>
      {connectionOpen && (
        <ConnectionDialog
          token={token}
          onClose={() => setConnectionOpen(false)}
          onSave={(value) => {
            setToken(value);
            setConnectionOpen(false);
          }}
        />
      )}
    </section>
  );
}
