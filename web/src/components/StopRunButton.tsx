import { useEffect, useState } from "react";
import { useApi, useRunControl } from "../api/hooks";
import { StopAuditsDialog } from "../dialogs/StopAuditsDialog";
import type { Run, RunStatus } from "../types";

export function StopRunButton({
  run,
  onStopped,
  variant = "default",
}: {
  run: { id: string; status: RunStatus };
  onStopped?: (run: Run) => void;
  variant?: "default" | "compact";
}) {
  const { token, epoch } = useApi();
  const [open, setOpen] = useState(false);
  const { mutate, busy, error } = useRunControl();
  useEffect(() => setOpen(false), [run.id, token, epoch]);
  if (run.status !== "queued" && run.status !== "running") return null;
  const compact = variant === "compact";

  async function stop() {
    if (await mutate<Run>(
      `/api/runs/${encodeURIComponent(run.id)}/cancel`, onStopped,
    )) {
      setOpen(false);
    }
  }

  return (
    <div className="stop-run-control" onClick={(event) => event.stopPropagation()}>
      <button
        type="button"
        className={`button secondary${compact ? " compact" : ""}`}
        aria-label={compact ? `Stop audit ${run.id}` : "Stop audit"}
        disabled={busy}
        onClick={() => setOpen(true)}
      >
        Stop audit
      </button>
      {open && (
        <StopAuditsDialog
          busy={busy}
          error={error}
          onClose={() => setOpen(false)}
          onConfirm={stop}
        />
      )}
    </div>
  );
}
