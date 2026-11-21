import { useEffect, useRef, useState, type FormEvent } from "react";
import { useApi } from "../api/hooks";
import { ApiError, errorMessage, importArticles } from "../api/client";
import type { ArticleImported, ServiceStatus } from "../types";
import { ErrorState } from "./ErrorState";

/** Importing reads the archive, so it exists only in live mode with archive reads on. */
export function archiveImportAvailable(status: ServiceStatus | null | undefined) {
  return status?.mode === "live" && status.archive_reads_enabled;
}

export function importSummary(result: ArticleImported) {
  return `Imported ${result.imported} · updated ${result.updated} · ` +
    `skipped ${result.skipped}`;
}

// Every message keeps the server's safe code, so the cause stays visible and searchable.
export function importError(error: unknown) {
  if (!(error instanceof ApiError)) return errorMessage(error);
  const suffix = error.code ? ` (${error.code})` : "";
  if (error.code === "archive_unavailable") {
    return "The archive could not be reached. Try again later (archive_unavailable).";
  }
  if (error.code === "untrusted_origin") {
    return "This page's address is not an allowed origin for the API; add it to " +
      "ALLOWED_ORIGINS on the server (untrusted_origin).";
  }
  if (error.status === 401 || (error.status === 403 && !error.code)) {
    return `Check the API token in Settings before importing.${suffix}`;
  }
  if (error.status === 422) return `Choose between 1 and 90 days.${suffix}`;
  return `${error.message}${suffix}`;
}

export function ArchiveImport({ onImported }: { onImported: () => void }) {
  const { token, epoch, status, articlesCache } = useApi();
  const [days, setDays] = useState("30");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState("");
  const [error, setError] = useState("");
  const controller = useRef<AbortController | null>(null);
  useEffect(() => {
    setBusy(false);
    setResult("");
    setError("");
    return () => controller.current?.abort();
  }, [token, epoch]);
  if (!archiveImportAvailable(status)) return null;
  const count = Number(days);
  const valid = Number.isInteger(count) && count >= 1 && count <= 90;

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (busy || !valid) return;
    controller.current?.abort();
    const current = new AbortController();
    controller.current = current;
    setBusy(true);
    setResult("");
    setError("");
    try {
      const imported = await importArticles(count, token, current.signal);
      if (current.signal.aborted) return;
      setResult(importSummary(imported));
      articlesCache.clear();
      onImported();
    } catch (failure) {
      if (!current.signal.aborted) setError(importError(failure));
    } finally {
      if (!current.signal.aborted) setBusy(false);
    }
  }

  return (
    <form className="archive-import" onSubmit={submit}
      aria-label="Import articles from the archive">
      <label>
        Days
        <input type="number" inputMode="numeric" min={1} max={90} step={1}
          value={days} onChange={(e) => setDays(e.target.value)}
          aria-describedby="archive-import-help" />
      </label>
      <button className="button secondary" type="submit" disabled={busy || !valid}>
        {busy ? "Importing…" : "Import from archive"}
      </button>
      <p className="help" id="archive-import-help">
        Reads removed articles from the archive for the last 1 to 90 days.
      </p>
      {result && <p className="help" role="status">{result}</p>}
      {error && <ErrorState message={error} />}
    </form>
  );
}
