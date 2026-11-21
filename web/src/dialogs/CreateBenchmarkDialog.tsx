import { useCallback, useState, useEffect, useRef, type FormEvent } from "react";
import { request, errorMessage } from "../api/client";
import type { Run } from "../types";
import { Icon } from "../components/Icon";
import { Modal } from "../components/Modal";
import { ErrorState as ErrorBox } from "../components/ErrorState";
import { VerdictSelect } from "../components/VerdictSelect";

export function CreateBenchmarkDialog({
  token,
  onClose,
  onCreated,
}: {
  token: string;
  onClose: () => void;
  onCreated: (run: Run) => void;
}) {
  const [url, setUrl] = useState("");
  const [title, setTitle] = useState("");
  const [content, setContent] = useState("");
  const [label, setLabel] = useState("");
  const [expected, setExpected] = useState("");
  const [published, setPublished] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const example = useCallback(() => {
    setUrl(`https://example.invalid/synthetic-case-${Date.now()}`);
    setTitle("Synthetic community notice");
    setContent(
      "This is a synthetic article created for a local workflow demonstration. " +
        "A fictional community center announces a public meeting about its " +
        "garden. Residents are invited to discuss opening times, volunteer " +
        "schedules, and the planting calendar. No real person, organization, " +
        "or news event is represented in this text. The notice is intended " +
        "solely to exercise the audit interface and inspect the complete " +
        "workflow trace.",
    );
    setLabel("Synthetic walkthrough");
    setExpected("");
    setPublished("");
  }, []);
  const controller = useRef<AbortController | null>(null);
  useEffect(() => () => controller.current?.abort(), []);

  async function submit(event: FormEvent) {
    event.preventDefault();
    controller.current?.abort();
    const current = new AbortController();
    controller.current = current;
    setSaving(true);
    setError("");
    try {
      const run = await request<Run>("/api/benchmarks/runs", token, {
        signal: current.signal,
        method: "POST",
        body: JSON.stringify({
          removed_article: {
            url: url.trim(),
            title: title.trim(),
            content: content.trim(),
            ...(published
              ? { published_at: new Date(published).toISOString() }
              : {}),
          },
          similar_articles: [],
          ...(label.trim() ? { label: label.trim() } : {}),
          ...(expected ? { expected_verdict: expected } : {}),
        }),
      });
      if (!current.signal.aborted) onCreated(run);
    } catch (error) {
      if (!current.signal.aborted) setError(errorMessage(error));
    } finally {
      if (!current.signal.aborted) setSaving(false);
    }
  }
  return (
    <Modal title="New benchmark" onClose={onClose} wide>
      <form onSubmit={submit}>
        <div className="modal-body">
          <div className="modal-intro">
            <p>
              Submit a controlled case to the current workflow. The complete
              input, prediction, and trace stay together.
            </p>
            <button type="button" className="text-button" onClick={example}>
              Use a synthetic example <Icon name="arrow" size={13} />
            </button>
          </div>
          <label>
            Article URL
            <input
              type="url"
              required
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              placeholder="https://…"
              autoFocus
            />
          </label>
          <label>
            Article title
            <input
              required
              maxLength={2000}
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              placeholder="Title of the removed article"
            />
          </label>
          <label>
            Article content
            <textarea
              required
              maxLength={250000}
              rows={7}
              value={content}
              onChange={(e) => setContent(e.target.value)}
              placeholder="Paste the article text to audit…"
            />
          </label>
          <div className="form-row">
            <label>
              Case label <span>(optional)</span>
              <input
                maxLength={200}
                value={label}
                onChange={(e) => setLabel(e.target.value)}
                placeholder="A useful name for this case"
              />
            </label>
            <label>
              Expected verdict <span>(optional)</span>
              <VerdictSelect value={expected} onChange={setExpected} />
            </label>
          </div>
          <label>
            Published at <span>(optional, local time)</span>
            <input
              type="datetime-local"
              value={published}
              onChange={(e) => setPublished(e.target.value)}
            />
          </label>
          {error && <ErrorBox message={error} />}
          <p className="form-note">
            <Icon name="grid" size={15} /> This creates a benchmark. Live audits
            enter through the API or webhook.
          </p>
        </div>
        <div className="modal-footer">
          <button className="button secondary" type="button" onClick={onClose}>
            Cancel
          </button>
          <button className="button primary" type="submit" disabled={saving}>
            {saving ? (
              <>
                Starting…
              </>
            ) : (
              <>
                <Icon name="plus" /> Run benchmark
              </>
            )}
          </button>
        </div>
      </form>
    </Modal>
  );
}
