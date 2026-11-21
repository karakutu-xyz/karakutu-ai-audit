import type { ArticleStatus } from "../types";

export const articleStatuses: Record<ArticleStatus, string> = {
  not_audited: "Not audited",
  in_progress: "In progress",
  audited: "Audited",
  failed: "Failed",
  cancelled: "Stopped",
};

export function ArticleStatusChip({ status }: { status: ArticleStatus }) {
  return (
    <span className={`chip ${status === "failed" ? "verdict-failure" : "status-chip"}`}>
      {articleStatuses[status]}
    </span>
  );
}
