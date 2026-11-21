import { Modal } from "../components/Modal";
import { ErrorState } from "../components/ErrorState";

export function StopAuditsDialog({
  queued = false,
  busy,
  error,
  onClose,
  onConfirm,
}: {
  queued?: boolean;
  busy: boolean;
  error: string;
  onClose: () => void;
  onConfirm: () => void;
}) {
  return (
    <Modal
      title={queued ? "Stop queued audits?" : "Stop this audit?"}
      onClose={onClose}
    >
      <div className="modal-body">
        <p>
          {queued &&
            "Every queued audit will be stopped. Running audits continue. "}
          Steps already recorded are kept. This is not a result.
        </p>
        {error && <ErrorState message={error} />}
      </div>
      <div className="modal-footer">
        <button type="button" className="button secondary" onClick={onClose}>
          Keep {queued ? "audits" : "audit"}
        </button>
        <button
          type="button"
          className="button primary"
          disabled={busy}
          onClick={onConfirm}
        >
          {busy ? "Stopping…" : queued ? "Stop queued" : "Stop audit"}
        </button>
      </div>
    </Modal>
  );
}
