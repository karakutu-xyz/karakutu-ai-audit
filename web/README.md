# Audit workbench

React + TypeScript + Vite, using the Newsroom ledger design. The browser connects
only to same-origin `/api` routes; Vite proxies to `127.0.0.1:2200` by default.
`KARAKUTU_API_PROXY` can select another local API for development. Development
binds to loopback on port 2201. Fonts are bundled locally from the installed
Source Serif 4, Inter and JetBrains Mono variable font packages; no CDN is used.

```sh
npm run dev
npm run typecheck
npm run build
npm run check:contrast
npm run check:ui
```

## Routes

`src/router.tsx` implements History API navigation, `useRoute`, `navigate` and
real anchor `Link` elements. Only ordinary same-origin left clicks are
intercepted, preserving modified clicks, downloads and explicit targets.

| Address | Page |
| --- | --- |
| `/` | Replaces the current history entry with `/articles` |
| `/articles` | Article ledger (start page) |
| `/articles/:ref` | Article story, evidence, review, history and audit action |
| `/runs` | Benchmark and live run ledger |
| `/runs/:id` | Standalone run evidence, records and human review |
| `/benchmarks` | Benchmark ledger and creation dialog |
| `/settings` | Connection, capabilities and read-only workflow |
| Other paths | Not found with a link to Runs |

Page titles include the page and, for detail, the run id. Vite and Nginx retain
SPA fallback for direct links. Run ledger `origin`, `verdict`, `status`,
`workflow_id` and `offset` live in the query string. Filter edits replace history;
paging adds history entries. Back/forward restores the query and each history
entry's saved scroll position without moving focus. The router owns manual
scroll restoration: ledger restoration waits for cached or fresh rows, run detail
waits for its run, and Settings waits for service and workflow content. Errors
end the wait, and an unresolved restoration expires after five seconds so later
content cannot move the user. New page navigation scrolls to the top and focuses
the main region. The run link carries
the previous ledger address in history state so its Back link restores filters.
A directly opened run defaults to a Back to Runs link.

The origin filter maps Benchmark/Live (all) to API `category` and API/Webhook/
Article audit to API `origin`; All omits both. The Benchmarks page always forces
`category=benchmark` and hides the origin filter. The API also handles status,
verdict, workflow version, a 50-run limit and offset, so totals and navigation
match every filter. Changing a filter resets the offset.
Only a fresh response for the current server query can replace an offset past
the result set with the last valid page, or drop it for an empty result set.
Cached totals never correct paging or block revalidation. A mode/token change
resets ledger paging, including
when the ledger is next opened after a switch on another page.

The article title is the row's semantic link; ordinary clicks elsewhere on the
row open that run while preserving the ledger address. Modified clicks and
controls keep their native behavior. The run id and any distinct case label share
one metadata line; labels that repeat the title, including prefixed copies, are
omitted from display. `src/format.ts` owns this comparison and duration formatting
for the ledger, run facts and steps. Saved records remain unchanged.

Article filters `q`, `status`, `verdict`, `category` and `offset` live in the query
string and all filter on the server. Category defaults to All, which omits the API
category parameter; Live articles and Benchmark cases are explicit choices.
Each ledger keeps its own epoch marker so visiting Runs after a service switch
cannot consume the Articles paging reset. Empty results check the unfiltered
inventory to distinguish no articles from no matches. Clearing filters shows All.
Article row links retain the ledger address for the All articles link.

## Structure

- `src/main.tsx`: local fonts and styles, React entry point.
- `src/App.tsx`: provider, shell and page routing.
- `src/api/client.ts`: the sole HTTP boundary, fixed error messages and bearer
  headers; never logs or exposes raw upstream error payloads.
- `src/api/context.tsx`: token in React memory, service status and cache generation.
- `src/api/hooks.ts`: cancellable run/list/workflow reads and polling.
- `src/types.ts`: existing public API shapes.
- `src/components/`: shell/navigation, verdict/status chips, JSON copy inspector,
  native modal, facts, icons and readable empty/loading/error states.
  `StopRunButton` owns individual stop confirmation, busy/error feedback and the
  cancel request; pass `run` and optional `onStopped(run)`. Its compact variant
  labels each ledger action with the run id and contains clicks within the control.
  `QueueIndicator` shows shell state and links to Runs; `QueueBar` owns the only
  Pause/Resume and confirmed Stop queued controls. Both stop paths share
  `StopAuditsDialog`.
- `src/pages/`: Articles, article detail, Runs, run detail, Benchmarks and Settings.
- `src/features/review/`: append-only human reactions and draft form.
- `src/features/articles/`: collapsed article text and saved snapshot versions.
- `src/features/steps/`: story, stored evidence projections, exact step records and model calls.
- `src/features/workflow/`: read-only preserved source/settings inspector.
- `src/dialogs/`: benchmark creation and connection forms.
- `src/styles/`: tokens, base rules, shell, ledger, detail and forms.

## Behavior

Run detail and ledger rows refresh after stopping. Cancelled runs show "Stopped
by a person", the saved story headline and no verdict chip or tone. Interrupted
`CancelledError` steps show neutral "Stopped"; expanded JSON retains the original
failed record. Unreached steps remain pending.

Queue reads poll every 1.5 seconds while active or paused and every 30 seconds
while idle in a visible tab. Successful controls invalidate queue reads at once.
The shell indicator remains available on every page, with counts, Queue paused
or Idle; its link opens the single control bar on Runs. Queue reads and open stop
dialogs reset when the token or service epoch changes.

Lists load immediately and poll every four seconds while the tab is visible.
Queued/running article and run detail poll every 1.5 seconds while visible; polling stops when
the run completes. Refresh reloads a completed run. Reads abort on unmount,
parameter changes, token changes and mode resets; polling prevents overlap.
Manual refresh and polling retain data for the same resource path, token and
service epoch and expose `refreshing`, shown quietly beside Refresh. Ledger
responses are also retained per server query in provider memory for that epoch,
with at most 20 entries and least recently used queries evicted first. Returning
to a loaded query renders its rows immediately and revalidates in the
background. An uncached path, token or service epoch change immediately clears
previous data and shows the initial loading state, even before reload effects
run. Failed same-epoch refreshes keep the last loaded record visible with an
error and retry action, including workflow snapshots. Failed reloads after an
epoch change never show the previous epoch's data.

Service status is read immediately and every five seconds, including during
run polling and on other pages. An outage makes the pill say mode unknown,
while retaining the last known mode internally. When a later response reports a
change between test/live, reads reload and review/benchmark drafts reset.
The page stays mounted; its reads discard all data from the previous epoch.
The mode pill stays in the sticky top bar. Service capabilities and
the in-memory API token control live in Settings; 401 errors link there.
Reloading clears the token; nothing uses local/session storage.

A run's immutable workflow id loads independently of run polling and has a
separate retry action. The current definition is a fallback only when its id
matches the run. A version link opens `/settings?workflow_id=…`; the snapshot is
also inspectable inline. Step buttons expose their expanded state and control
the corresponding detail row. Expanded steps show exact input/output JSON
directly, with separate copy controls and internal scrolling; evidence and model
calls retain their disclosures. JSON panels sit side by side from 1000px and
stack below. The run's model name takes precedence over distinct step models;
missing model metadata says “Not recorded”, and steps with no calls say
“No model call”. Exact saved inputs, findings, outputs, evidence and model
records are preserved. The verdict area contains the single authored
“not a finding of censorship” line; stored source/JSON may include its original
guardrail wording.

Article detail reads `/api/articles/:ref` and the latest `/api/runs/:id` through
hooks. Its story uses the latest computed `RunStory`, preferring the full run's
summary as its records arrive. Completed steps show footnotes derived only from
recognized fields in that run's stored step output/evidence: fetched URLs and
HTTP status, containment, search matches, attribution quotes and related removals.
Failed/skipped steps keep their stored reasons. A null or failed original probe
and a dead cited source never mark the original URL unavailable; that note needs
an explicit 404/410 original probe record. No archive link is fabricated.

The primary action posts through `api/client.ts`. A successful response inserts
the new run into the displayed history immediately and refetches the article,
restarting polling. A conflict explains that an audit is already running and
refetches; access errors point to Connection settings; disabled live audits get
a fixed message. Raw error bodies are never shown. Token/epoch changes abort
pending submissions and reset action state. Review uses the exact latest run;
run detail links its title back when `article_ref` is present. Removed text is
clamped to twelve rendered lines, with a toggle only when it overflows; snapshot
versions show their saved titles and check dates. The rail follows the story
below 1000px and sits beside it on wider screens.

Benchmark creation always posts to `/api/benchmarks/runs` and opens the new run.
The synthetic-example button generates fictional content only when clicked.
Reviews preserve Agree/Disagree/Needs review, reviewer, optional note, optional
expected verdict and saved reactions. Expanding steps or records preserves the
draft. Switching runs or service mode clears it. Export downloads the existing
`/api/runs/:id/export` JSON payload.

`npm run check:ui` runs offline checks using installed TypeScript and React:
route matching, history navigation, anchor/row click behavior, ledger filters,
static run-detail markup, readable states, duration/label formatting and model
fallbacks. A small hook harness exercises the actual data hooks with a fake
transport and timers to verify refresh retention, cancellation and polling.
It checks that mode/token changes clear lists, runs and workflows before effects
run, that failed epoch reloads keep them empty, and that same-epoch refresh
errors retain data. Static markup checks cover workflow errors alongside a
retained snapshot and its retry control.
History checks cover saving scroll before navigation, Back/Forward restoration,
focus retention, waiting for ledger/run/Settings content, terminal errors and the
five-second restoration deadline. Ledger checks cover query caches across
remounts, LRU eviction at 20 entries, cache isolation on mode/token changes,
fresh-response offset correction (including a cached total of 27 followed by
51 at offset 50) and paging resets after a service switch.
Article checks cover the root redirect, server-side filters and category default,
native row clicks, accepted-run history, first/re-run/running labels, safe
409/401/403/404 handling, submission cancellation, polling completion, query-cache
isolation and stored-only evidence, including the dead-original guard.
It performs no HTTP requests. Static markup checks do not replace browser layout
validation.

## Visual conventions and accessibility

Declare colours and shared typography, spacing, rules and radii only in
`styles/tokens.css`; area styles consume custom properties. Source Serif 4 is
used for page and verdict headlines, Inter for the interface, and JetBrains
Mono for ids, hashes, JSON and durations. Body text is at least 14px and labels
at least 12px. `VerdictChip` is the single verdict label/tone mapping.

`npm run check:contrast` reads the real token colours, computes WCAG
relative luminance for every declared text/background pair and names the
interactive states using each pair: links/navigation, skip link, primary and
secondary buttons, text/copy/retry controls, selected reactions, ledger row
hover/focus, step disclosures and form controls, plus all static chip tones.
It fails below 4.5:1. Add a pair/state when introducing a new one; focus outlines
do not change text colours, and transparent controls use their parent's surface.
For the skip link, the checker resolves the winning simple anchor rules from
the stylesheets, so generic hover rules cannot silently invalidate its pairs.

The shared breakpoint comment in `base.css` names wide (1000px), compact (720px),
ledger (620px) and narrow (480px). At 620px and below, ledger filters use two
columns; the workflow field spans both on Benchmarks. Refresh stays beside
the page title.
At phone widths navigation has its own scrollable row, facts/forms stack, and
tables scroll inside a labelled, keyboard-focusable region. The page caps at
1240px and the ledger has a 1080px minimum width. Visible focus, text
chips, labelled inputs, native dialog focus containment/Escape, a skip link and
reduced-motion rules are retained. No overall verdict confidence is invented.
