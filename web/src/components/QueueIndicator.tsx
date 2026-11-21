import { useQueue } from "../api/hooks";
import { Link } from "../router";

export function QueueIndicator() {
  const { data } = useQueue();
  const label = !data
    ? "Queue unavailable"
    : data.paused
      ? "Queue paused"
      : data.running.length || data.queued.length
        ? `${data.running.length} running · ${data.queued.length} queued`
        : "Idle";
  return (
    <Link href="/runs" className="queue-indicator" aria-label={`Audit queue: ${label}`}>
      <span role="status">{label}</span>
    </Link>
  );
}
