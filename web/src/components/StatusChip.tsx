import { humanize } from "../format";

export function StatusChip({ status }: { status: string }) {
  return (
    <span
      className={`chip ${["failed", "error"].includes(status) ? "verdict-failure" : "status-chip"}`}
    >
      {humanize(status)}
    </span>
  );
}
