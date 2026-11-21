import { useState } from "react";
import { useQueue, useRunControl } from "../api/hooks";
import { StopAuditsDialog } from "../dialogs/StopAuditsDialog";
import { ErrorState } from "./ErrorState";
import { Loading } from "./Loading";

export function QueueBar({
  queue,
  onChanged,
}: {
  queue: ReturnType<typeof useQueue>;
  onChanged: () => void;
}) {
  const [confirm, setConfirm] = useState(false);
  const { mutate, busy, error } = useRunControl();
  async function act(action: "pause" | "resume" | "cancel") {
    if (await mutate(`/api/queue/${action}`)) {
      setConfirm(false);
      queue.refresh();
      onChanged();
    }
  }
  return (
    <section className="queue-bar" aria-label="Audit queue">
      {queue.error && (
        <ErrorState message={queue.error} onRetry={queue.refresh} />
      )}
      {!queue.data && queue.loading && <Loading>Loading queue…</Loading>}
      {queue.data && (
        <div className="queue-controls">
          <p role="status">
            {queue.data.running.length} running · {queue.data.queued.length} queued
            {queue.data.paused && " · Queue paused"}
          </p>
          <div className="actions">
            <button
              className="button secondary"
              disabled={busy}
              onClick={() => act(queue.data!.paused ? "resume" : "pause")}
            >
              {queue.data.paused ? "Resume" : "Pause"}
            </button>
            <button
              className="button secondary"
              disabled={busy || queue.data.queued.length === 0}
              onClick={() => setConfirm(true)}
            >
              Stop queued
            </button>
          </div>
        </div>
      )}
      {error && !confirm && <ErrorState message={error} />}
      {confirm && (
        <StopAuditsDialog
          queued
          busy={busy}
          error={error}
          onClose={() => setConfirm(false)}
          onConfirm={() => act("cancel")}
        />
      )}
    </section>
  );
}
