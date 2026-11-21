import { useState, useEffect, useRef, type FormEvent } from "react";
import { request, errorMessage } from "../../api/client";
import type { Run, ReactionKind } from "../../types";
import { Icon } from "../../components/Icon";
import { Badge } from "../../components/Badge";
import { ErrorState as ErrorBox } from "../../components/ErrorState";
import { VerdictSelect } from "../../components/VerdictSelect";
import { verdictLabel } from "../../components/VerdictChip";
import { humanize, date } from "../../format";

export function Review({
  run,
  token,
  onSaved,
}: {
  run: Run;
  token: string;
  onSaved: (run: Run) => void;
}) {
  const [reaction, setReaction] = useState<ReactionKind | "">("");
  const [reviewer, setReviewer] = useState("");
  const [note, setNote] = useState("");
  const [expected, setExpected] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [saved, setSaved] = useState(false);
  const controller = useRef<AbortController | null>(null);
  useEffect(() => () => controller.current?.abort(), []);

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!reaction || !reviewer.trim()) return;
    controller.current?.abort();
    const current = new AbortController();
    controller.current = current;
    setSaving(true);
    setError("");
    setSaved(false);
    try {
      const result = await request<Run["reactions"][number]>(
        `/api/runs/${encodeURIComponent(run.id)}/reactions`,
        token,
        {
          signal: current.signal,
          method: "POST",
          body: JSON.stringify({
            reaction,
            reviewer: reviewer.trim(),
            note: note.trim(),
            ...(expected ? { expected_verdict: expected } : {}),
          }),
        },
      );
      if (current.signal.aborted) return;
      onSaved({ ...run, reactions: [...run.reactions, result] });
      setReaction("");
      setNote("");
      setExpected("");
      setSaved(true);
    } catch (error) {
      if (!current.signal.aborted) setError(errorMessage(error));
    } finally {
      if (!current.signal.aborted) setSaving(false);
    }
  }

  return (
    <section className="review-section">
      <div className="subsection-title">
        <h3>Human review</h3>
        <Badge>
          {run.reactions.length}{" "}
          {run.reactions.length === 1 ? "reaction" : "reactions"}
        </Badge>
      </div>
      <p className="help">
        Record your judgment against this exact run and workflow version.
      </p>
      <form onSubmit={submit}>
        <div
          className="reaction-options"
          role="group"
          aria-label="Your reaction"
        >
          {(["agree", "disagree", "needs_review"] as const).map((value) => (
            <button
              type="button"
              key={value}
              className={
                reaction === value
                  ? `reaction-button active ${value}`
                  : "reaction-button"
              }
              aria-pressed={reaction === value}
              onClick={() => {
                setReaction(value);
                setSaved(false);
              }}
            >
              <Icon
                name={
                  value === "agree"
                    ? "thumb"
                    : value === "disagree"
                      ? "close"
                      : "flag"
                }
                size={15}
              />
              {humanize(value)}
            </button>
          ))}
        </div>
        {reaction && (
          <div className="review-form">
            <div className="form-row">
              <label>
                Reviewer
                <input
                  required
                  maxLength={120}
                  value={reviewer}
                  onChange={(e) => setReviewer(e.target.value)}
                  placeholder="Your name or identifier"
                  autoComplete="name"
                />
              </label>
              <label>
                Expected verdict <span>(optional)</span>
                <VerdictSelect value={expected} onChange={setExpected} />
              </label>
            </div>
            <label>
              Review note <span>(optional)</span>
              <textarea
                value={note}
                maxLength={4000}
                onChange={(e) => setNote(e.target.value)}
                placeholder="What evidence informed your judgment?"
                rows={3}
              />
            </label>
            <button
              className="button primary small"
              type="submit"
              disabled={saving || !reviewer.trim()}
            >
              {saving ? "Saving…" : "Save review"}
              <Icon name="arrow" size={15} />
            </button>
          </div>
        )}
      </form>
      {error && <ErrorBox message={error} />}
      {saved && (
        <p className="saved-message" role="status">
          <Icon name="check" size={15} /> Review saved to this workflow version.
        </p>
      )}
      {run.reactions.length > 0 && (
        <div className="review-history">
          {[...run.reactions].reverse().map((item) => (
            <article className="review-item" key={item.id}>
              <div>
                <strong>{item.reviewer}</strong>
                <Badge
                  tone={
                    item.reaction === "agree"
                      ? "teal"
                      : item.reaction === "disagree"
                        ? "red"
                        : "amber"
                  }
                >
                  {humanize(item.reaction)}
                </Badge>
                <time dateTime={item.created_at}>{date(item.created_at)}</time>
              </div>
              {item.note && <p>{item.note}</p>}
              {item.expected_verdict && (
                <p className="help">
                  Expected: {verdictLabel(item.expected_verdict)}
                </p>
              )}
            </article>
          ))}
        </div>
      )}
    </section>
  );
}
