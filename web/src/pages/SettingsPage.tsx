import { useState } from "react";
import type { ArchiveState, SearchState } from "../types";
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
};

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
              label: "Live ingestion",
              value: status.live_ingestion_enabled
                ? "Enabled"
                : "Disabled · live runs require API or webhook ingestion",
            },
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
