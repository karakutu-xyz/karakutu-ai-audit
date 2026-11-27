import { useEffect, useState } from "react";
import { useApi, usePollingControl, usePollingSettings } from "../../api/hooks";
import type {
  PollingStatus, PollSettingName, PollSettings, PollValues, RunMode,
} from "../../types";
import { ErrorState } from "../../components/ErrorState";
import { Loading } from "../../components/Loading";

const numbers = [
  { name: "interval_minutes", label: "Interval (minutes)", max: 1440 },
  { name: "lookback_days", label: "Lookback (days)", max: 30 },
  { name: "auto_audit_max_per_poll", label: "Auto-audit batch size", max: 50 },
] as const;

export function PollingControls({ polling, mode }: {
  polling: PollingStatus; mode: RunMode;
}) {
  const { token, epoch } = useApi();
  const settings = usePollingSettings();
  // Drafts contain only edited values, so saving one field preserves other sources.
  const [draft, setDraft] = useState<Partial<PollValues>>({});
  const [saved, setSaved] = useState<{
    token: string; epoch: number; data: PollSettings;
  } | null>(null);
  useEffect(() => {
    setDraft({});
    setSaved(null);
  }, [token, epoch]);
  useEffect(() => { setSaved(null); }, [settings.data]);
  const control = usePollingControl((result) => {
    setSaved({ token, epoch, data: result });
    settings.refresh();
  });
  const local = saved?.token === token && saved.epoch === epoch ? saved.data : null;
  const data = local ?? settings.data;
  const running = polling.running || control.runPending;
  const testMode = mode === "test" || polling.reason === "test_mode";
  const unavailable = testMode || polling.reason === "archive_disabled";
  const reason = testMode ? "Polling is off in test mode."
    : polling.reason === "archive_disabled" ? "Enable archive reads to poll."
    : running ? "An archive poll is already running." : "";
  const state = testMode ? "Off in test mode"
    : polling.reason === "archive_disabled" ? "Off · archive reads disabled"
    : polling.paused ? "Paused" : "Running";

  function marker(name: PollSettingName) {
    if (!data) return null;
    return <div className="poll-setting-source">
      <span className="help">
        {data[name].source === "env" ? "From .env" : "Set here"}
      </span>
      <button type="button" className="button secondary compact"
        disabled={control.busy || data[name].source === "env"}
        aria-label={`Reset ${name.replaceAll("_", " ")} to default`}
        onClick={async () => {
          if (await control.mutate("reset", { names: [name] })) {
            setDraft((previous) => {
              const next = { ...previous };
              delete next[name];
              return next;
            });
          }
        }}>
        Reset to default
      </button>
    </div>;
  }

  return <section className="ruled-section" aria-labelledby="polling-title">
    <h2 id="polling-title">Polling</h2>
    <p role="status">{state}{running ? " · Polling now"
      : polling.next_poll_at ? ` · Next poll ${new Date(polling.next_poll_at)
        .toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}` : ""}</p>
    {settings.error && <ErrorState message={settings.error} onRetry={settings.refresh} />}
    {!data && !settings.error && <Loading>Loading polling settings…</Loading>}
    {data && <form onSubmit={async (event) => {
      event.preventDefault();
      if (await control.mutate("save", draft)) setDraft({});
    }}>
      <div className="poll-settings-grid">
        <div>
          <button type="button" className="button secondary"
            aria-pressed={!unavailable && data.polling_enabled.value}
            disabled={control.busy || unavailable}
            aria-describedby={unavailable ? "poll-now-reason" : undefined}
            onClick={() => void control.mutate("save", {
              polling_enabled: !data.polling_enabled.value,
            })}>
            {unavailable ? "Polling unavailable"
              : data.polling_enabled.value ? "Pause polling" : "Resume polling"}
          </button>
          {marker("polling_enabled")}
        </div>
        <div>
          <label className="poll-toggle">
            <input type="checkbox" checked={data.auto_audit_enabled.value}
              disabled={control.busy}
              onChange={(event) => void control.mutate("save", {
                auto_audit_enabled: event.target.checked,
              })} />
            Auto-audit
          </label>
          {marker("auto_audit_enabled")}
        </div>
        {numbers.map(({ name, label, max }) => <div key={name}>
          <label>{label} <span className="help">(1–{max})</span>
            <input type="number" min={1} max={max} step={1} required
              value={Number.isNaN(draft[name]) ? "" : draft[name] ?? data[name].value}
              disabled={control.busy}
              onChange={(event) => setDraft((previous) => ({
                ...previous, [name]: event.target.valueAsNumber,
              }))} />
          </label>
          {marker(name)}
        </div>)}
      </div>
      <div className="run-row-controls">
        <button className="button" disabled={control.busy
          || Object.keys(draft).length === 0}>Save</button>
        <button type="button" className="button secondary" disabled={control.busy
          || !Object.values(data).some((setting) => setting.source === "ui")}
          onClick={async () => {
            if (await control.mutate("reset", { names: [] })) setDraft({});
          }}>
          Reset all to default
        </button>
        <button type="button" className="button secondary"
          disabled={!!reason} aria-describedby="poll-now-reason"
          onClick={() => void control.mutate("run")}>
          Poll now
        </button>
      </div>
    </form>}
    <p id="poll-now-reason" className="help">{reason
      || "Poll now runs once, including while paused. Queued audits keep running."}</p>
    {control.error && <p className="error-box" role="alert">{control.error}</p>}
  </section>;
}
