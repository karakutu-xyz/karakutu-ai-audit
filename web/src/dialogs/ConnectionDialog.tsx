import { useState } from "react";
import { Modal } from "../components/Modal";

export function ConnectionDialog({
  token,
  onClose,
  onSave,
}: {
  token: string;
  onClose: () => void;
  onSave: (value: string) => void;
}) {
  const [value, setValue] = useState(token);
  return (
    <Modal title="Connection settings" onClose={onClose}>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          onSave(value.trim());
        }}
      >
        <div className="modal-body">
          <p className="help">
            The workbench connects to the local audit API. If API authentication
            is enabled, enter the configured token.
          </p>
          <label>
            API token <span>(optional)</span>
            <input
              type="password"
              autoComplete="off"
              value={value}
              onChange={(e) => setValue(e.target.value)}
              placeholder="Bearer token"
              autoFocus
            />
          </label>
          <p className="form-note">
            Kept in memory for this tab only. Reloading clears it.
          </p>
          <div className="connection-address">
            <span>API route</span>
            <code>/api → 127.0.0.1:2200</code>
          </div>
        </div>
        <div className="modal-footer">
          <button className="button secondary" type="button" onClick={onClose}>
            Cancel
          </button>
          <button className="button primary" type="submit">
            Apply connection
          </button>
        </div>
      </form>
    </Modal>
  );
}
