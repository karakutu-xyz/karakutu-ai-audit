import type { ServiceStatus } from "../types";

export function ModePill({ status }: { status: ServiceStatus | null }) {
  return (
    <span className="mode-pill" role="status">
      {status?.mode === "test"
        ? "Test mode · demo data"
        : status?.mode === "live"
          ? "Live"
          : "Service mode unknown"}
    </span>
  );
}
