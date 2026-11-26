# Workflow and iteration

`backend/audit/engine.py` builds a LangGraph `StateGraph` with six named steps and conditional edges. `workflow/` owns domain logic and prompts; `search/` owns measured text matching and guarded page fetching. The original evidence logic is retained instead of replacing calibrated rules with model opinion.

| Step | Input and action | Output / branch |
| --- | --- | --- |
| 1. Classification | Article text plus catalog-grounded IPTC topic/genre menus. Coarse then fine model calls. | Up to five validated tags, genre, scope and gist. Unavailable catalog or failed classification never decides the audit. |
| 2. Similarity | Archive similarity candidates, fetched through the public-address guard. | Measured same-publisher republication ends as `MOVED` when at least 99% of the original is contained. Remaining steps are recorded as skipped. Measured partial copies and dated headline-and-word rewrite candidates are retained for the low-interest exit or web search. |
| 3. Relevance | Title and the first 2,000 characters of the article prose (archive HTML is reduced to its paragraphs first, as in the other steps). | Public-affairs judgment: laws, votes, budgets, public services, elections, rights and the use of power are in scope with or without an allegation of wrongdoing; private matters of influential people are out unless the story shows a public stake. A failed model call is `AUDIT_FAILED`; a low-interest judgment waits for step 4 before exiting as `LIKELY_MOVED` or `LIKELY_LOW_PUBLIC_INTEREST`. |
| 4. Related removals | Archive removal window (±14 days), same-subject model matching. | At least three confirmed related items across at least two publishers, or a related item already marked censored by a human auditor, raise `RELATED_REMOVALS`, keeping an otherwise low-interest item in scope. Without a pattern, a measured same-publisher partial copy holding at least 50% of the original text gives `LIKELY_MOVED`; otherwise a qualifying same-publisher rewrite gives `LIKELY_MOVED`, then a below-floor partial copy adds a relocation note to `LIKELY_LOW_PUBLIC_INTEREST`. Missing archive evidence does not manufacture a pattern. Strong key terms are lone acronyms (not all-caps header runs or labels) and two-word names (not company or legal-form pairs such as "Anonim Şirketi"); a name matches only from a word start. |
| 5. Source | Original URL, article citations, source search. | A live original with measured text overlap ends as `NOT_REMOVED`. A cited-source 404/410 may supply `SOURCE_DEAD`; access blocks and fetch refusals cannot. A live cited page counts as the same story, and becomes a web-search lead, only when it holds at least 20% of the archived text (the sibling line); below that it is reported as not shown to be the same story. |
| 6. Web search | Search queries, archive/source leads, fetched page evidence and structured model outputs. | Prompts preserve the first budgeted live pages and append the best rewrite if absent. Models identify candidate matches; measured grounding first applies the existing relocation rule, promoting a demoted rewrite only if no other same-publisher match qualifies. The deterministic decision function selects the verdict. All failed model attempts mean `AUDIT_FAILED`. |

`workflow/pipeline.py::_decide` maps substantive evidence to `NOT_REMOVED`, `MOVED`,
`LIKELY_MOVED`, `FOUND_ONLINE`, `POSSIBLE_CENSORSHIP`, or `INCONCLUSIVE`. The pipeline
also assembles the low-interest exit. First, the best live, measured same-publisher
partial copy from step 2 gives `LIKELY_MOVED` when it holds at least 50% of the
removed article's text (`old_in_new`). This starting floor is to be set from the
benchmark. Otherwise, a qualifying same-publisher rewrite gives `LIKELY_MOVED`.
Otherwise, a below-floor partial copy adds a relocation note to
`LIKELY_LOW_PUBLIC_INTEREST`, rounding its share down. Siblings without a rewrite
signal, unmeasured fits without that signal, other publishers and redirects ending
on another host do not decide this exit. Without a qualifying candidate, the
low-interest response is unchanged. Source and web search are skipped, so no model
compares key facts on this path; a person should compare the pages. This path never
gives `POSSIBLE_CENSORSHIP`. The engine handles failed-execution exits. The eight
verdict names remain compatible with the archived auditor. Every
`POSSIBLE_CENSORSHIP` explanation says it is not a finding of censorship and requires
human review. A missing search hit is not evidence of censorship.

Same-publisher rewrites can preserve facts while changing nearly every five-word
phrase. `search/rewrite.py` therefore measures headline words and distinctive words
(at least five characters), using the same Turkish case folding as text matching.
The removed article's own words are those covered by shingles surviving boilerplate
cleaning; if cleaning is capped or leaves nothing, all its distinctive words count.
These words are looked for throughout the candidate page. For at least 20 own words,
a rewrite needs at least 35% of those words, at least 35% of the original headline
words and a publication gap of at most two days. Shorter originals instead need
at least 80% headline overlap in both directions within one day, with at least 80%
of the original headline words present in the live page's text. For both rules, the
page must not redirect to a different page: a final URL, when present, must normalize
to the requested URL or identify the same article variant. All bars include
the boundary. A missing title falls back to the URL's final path segment, dropping
all-digit slug tokens. The candidate uses its archive title, else the URL slug.
Dates use the two publication dates when parseable, comparing calendar
dates regardless of timezone; otherwise only an explicitly supplied archive day gap
counts. Missing dates fail closed. Archive title and URL similarity scores are not
used, and verbatim copies remain republications rather than rewrites.

Similarity retains the best rewrite by word overlap, then headline overlap (headline
for short originals), unless a measured republication was found. It hands only that
best rewrite to web search. Each prompt preserves the first `max_pages` live pages
in their unchanged order and appends the rewrite page only if absent, giving at
most `max_pages + 1` pages. This also applies to every smaller prompt retry, so a
rewrite never pushes an otherwise-visible partial copy out of the prompt.
In scope, the model must still identify the page as the same story. Grounding first
applies the existing relocation rule in model-match order, demoting same-publisher
hits whose shingle containments are both below 20%. Only if that selection found no
relocation does it promote the first demoted rewrite, removing it from sibling
context and retaining its measurements and model-reported key facts and omissions.
A rewrite never pre-empts another match that the existing rule would select.
A selected relocation that also qualifies as a rewrite retains its rewrite details.
With key facts covered, findings give the headline and word shares rounded down,
publication gap and differing wording as `LIKELY_MOVED`; short originals say their
text was too short to compare. Missing key facts retain the existing
`POSSIBLE_CENSORSHIP` review path. Numeric rewrite evidence is saved in stage details.
No new flags are added. A pure paraphrase retaining too few distinctive words stays
set aside: lowering that bar would also admit unrelated pages.

Similarity and web-search overlap bars use prose after dropping paragraphs shared
by two other live same-site articles. Both requested URLs and fetched destinations
must stay on the removed article's publisher to vote or be measured as same-publisher.
Independence uses whole-page five-token shingles: each voter must have at least 30
shingles absent from both the removed article and the other voter, and at least half
as many as the distinct shingles within their shared paragraph keys. Shared volume
is symmetric; repeated paragraphs and page order cannot inflate it. The removed
article never votes, and reflowed copies cannot vote each other's body out.
Learned paragraphs are found as token sequences anywhere in a page (every occurrence,
overlapping ones included), and each side's own text, the shingles touching none of them,
is looked for in the whole other side. A footer then cannot inflate either score, a
different paragraph break cannot make identical text differ, and cleaning cannot hide
the removed article's text that a candidate carries (for example after the site revised
its footer; an archived footer that no longer matches today's is simply not dropped).
The removed article loses a learned paragraph only when the candidate also carries it;
a learned paragraph the candidate lacks counts in the old-in-new score as missing text,
like any other. A background paragraph that follow-up stories repeat word for word is
learned, but its absence from a relocated copy can no longer be hidden by dropping it.
The same applies to furniture: a copy on a different page template, with another footer
or none, keeps the learned footer it lacks in its score (below the cap described next,
where it was dropped before) and may fall below the move bar.
If the learned text covers more than 50% of the removed article's shingles, neither
side is stripped: both scores stay raw and `boilerplate_capped` is true. This cap also
protects against copies with long distinct teasers voting most of the body into
boilerplate. With fewer than two other pages, scores stay raw
(see `search/boilerplate.py`). The bars
(99% move, 30% partial, 20% sibling) were rechecked on cleaned invented page families
(`backend/tests/test_boilerplate.py`) and kept; the original-URL check in the source step
(50%) is not cleaned.
Review details retain raw and cleaned scores, the cap flag and matched paragraph
counts in removed-article/candidate order, including pages set aside as siblings;
the removed article's count covers only learned lines the candidate carries. The cap
still weighs everything learned. When capped, counts show proposed removals; neither side was stripped.

Residual limitation: unrelated articles can quote the same lede and vote it into
boilerplate when it covers at most half the removed text. In the invented regression,
a genuine partial copy falls from 71/223 (31.84%) to 33/185 (17.84%) old-in-new after
the quoted lede is removed. Its reverse score is 13.04% when the rest is rewritten,
so it is demoted below the sibling bar. The cap does not protect every partial copy
or prevent every score increase below its limit.

## Web search engines

Web search goes through the bundled SearXNG (`searxng/settings.yml`, pinned image in
`compose.search.yaml`), which the app queries with `categories=general`. The settings keep
the image's default general web engines (Brave, DuckDuckGo, Google CSE and Startpage, plus
Wikipedia and Wikidata) and turn on one more: mwmbl, a small independent index. Most of the
default web engines refuse this machine at times ("too many requests" or a CAPTCHA); mwmbl
answered every neutral test query, so web search no longer depends on them alone. Its
results are of mixed quality and thin for Turkish queries.

Audit queries describe removed news, so the engine set is a privacy choice, not only a
quality one. Big providers that do answer from here (Yandex, Bing) are deliberately left
off. Adding an engine needs a decision; `backend/tests/test_searxng_engines.py` fails when
the settings change any engine besides turning on mwmbl. The settings file is part of the workflow
definition, so changing the engine set gives new runs a new workflow ID.

## Review a saved run

Open a run from the Runs ledger (`/runs/<id>`). The page puts the saved prediction and its findings first, then the run facts, the six workflow steps and an append-only human reaction (**Human review**). Reviewer identity is required; the note and expected verdict are optional.

Each step row opens to its recorded explanation, the **Step input** beside the exact system/user messages of each model call, output JSON, and **Evidence** and **Model calls** (prompts, model responses and fetch/search/archive evidence). **Article and records** shows the submitted text, with the exact run input and output on demand. **Saved workflow** opens the run's preserved definition, not the currently configured workflow. These are presentation changes only; stored runs and verdict rules are unchanged.

## Run lifecycle and operator controls

Live runs audit saved live articles: manual requests use origin `article`, and
opt-in automatic audits use origin `poll`. Benchmark submissions stay in the
benchmark category; historical `api` and `webhook` origins remain readable.
Archive polling registers metadata every five minutes by default, then optionally
queues oldest first attempts within the configured backlog budget. Any existing
run, including failed, stopped or interrupted work, prevents an automatic retry.
Polling uses the same text loader, article lock and submission path as Run audit;
workflow snapshots and verdict rules are unchanged. See
[Archive polling](data-and-api.md#archive-polling) for scheduling and status.

Submission stores `queued` before execution. A free concurrency slot moves a run
to `running`; a completed workflow becomes `succeeded`, or `failed` for
`AUDIT_FAILED`. Shutdown/restart still marks incomplete work failed with
`interrupted`; it never silently replays work.

A person can stop a queued or running audit from a Runs ledger row, run detail or the
[cancel API](data-and-api.md#run-and-queue-controls). It becomes terminal
`cancelled`, with no verdict or output, a finish time and a message explaining that a person stopped it.
Steps already appended remain exact records; steps never reached remain pending.
The story label is "Stopped by a person" and the headline is "A person stopped
this audit before it finished. This is not a result." There is no verdict, human
review note or flag sentence. A failed step whose recorded `details.error_type`
is `CancelledError` is presented as neutral `stopped`, excluded from failed-step
lists; its saved record stays `failed` unchanged. Other reached steps keep their
statuses and unreached steps are pending. A late engine completion cannot replace
a cancelled record. Submit a new run to retry.

The shell shows queue state on every page; its indicator links to Runs. The Runs
queue bar is the single place to pause/resume starting queued work and stop all queued
audits after confirmation. Pausing leaves running audits alone and accepts new
submissions into the queue. Pause resets to open on every service restart. The API
also supports bulk stopping running work with `include_running=true`.

Test mode uses the same stop path. `DemoEngine` yields during every step delay,
so stopping mid-step retains already-emitted steps and cancels the pending delay.
The seed includes a separate cancelled example alongside the existing queued,
running and service-interrupted examples.

## Exact review record

Each step stores lean inputs by name, output, duration, model calls and evidence events.
Article-reading steps record `article.ref: "run.input.removed_article"` with URL,
title, publication date, content character count and SHA-256, without article text.
Classification adds catalog topic/genre counts, serialized bytes and SHA-256;
similarity adds archive availability; relevance adds the prose head length.
Related removals adds the classification gist, relevance flag, text head lengths,
the actual archive anchor/window when available, and the publish date its model prompt
carried (the archive's date when its record was valid, otherwise the submitted one).
Source reads only the article.
Web search records lead URLs/titles, prefetched page counts, the best rewrite URL
and the cited dead-source URL. Skipped steps record their reason.
Earlier stage objects and the catalog are never copied into a step input.

A model call includes the actual system/user messages, response schema, sampling controls and token limit, the unmodified successful completion envelope, parsed result, returned model name and usage. Failed parsing retains the raw completion; transport errors retain an exception type only, never an upstream error body. Authentication headers and wallet material are excluded.

Search events retain query, provider results and failure type unchanged. Fetch events
retain requested URLs and per-page URL, final URL, status, title, text character count
and SHA-256, or the dead/unreadable reason. Archive events retain GET path, params,
status, retry status and error type; output becomes `{"summary": {...}}` with bytes
and SHA-256, audit-list item counts or article identity and text length/hash where
available. Hashes of text use UTF-8; JSON byte counts/hashes use compact, sorted,
non-ASCII-preserving UTF-8 JSON with non-finite numbers normalized to null.
Prefiltered related headlines remain in stage details and prompts only.
`RECORD_FULL_EVIDENCE=true` restores full archive/fetch bodies for explicit local
debugging; step inputs remain lean. The default is false. These private records exist for human review in the ignored local database. They are not log output. Network/SDK diagnostic logs are redacted; LangSmith tracing is explicitly disabled.

The run's input stores the submitted audit request once. Article text is not repeated
in step inputs, default evidence or details. Exact model prompts remain in
`model_calls`, making truncation and retry ladders visible. Neither recording mode
changes computation or prompts. Historical cumulative inputs and full evidence
remain readable without rewriting stored records or migrating the database. Error response bodies and secrets are deliberately excluded.

## Change a prompt or rule

The six editable system prompts come from the database, seeded byte-for-byte from `workflow/prompts.py` through `workflow/prompt_registry.py`. Default constants remain the source of truth. Prompt versions and activations are append-only; creating a version does not activate it. API writes require a configured bearer token even when local reads are open. See [the routes and validation contract](data-and-api.md). Prompt examples must remain fabricated: never quote a real headline or name a real person, place or event.

1. Create a prompt version through the API, retaining the required output tokens and guardrail section; activate it explicitly. For source defaults or deterministic rules, edit the relevant source constant deliberately. Every prompt change is a verdict-semantics change and needs a benchmark.
2. Run focused regression tests and complete local checks. Update this guide and the changelog when behavior or contracts change.
3. Activation/rollback immediately saves a new workflow snapshot and replaces the current workflow, without restarting. Activation reuses the source, dependencies and settings captured at startup; source edits need a restart. Format 3 fingerprints include the effective per-step model mapping and exact active prompt texts, source, locked dependencies, search configuration and behavior settings. Version IDs, authors, activation notes and timestamps do not affect the fingerprint. Returning to the same texts returns the same workflow ID when the rest of the definition is unchanged.
4. Each submission captures the workflow, prompts and models before queueing. Queued and running runs keep that set throughout execution; later submissions use the newly active set. Stage agents are private to each run. Historical inputs and workflow snapshots never change.
5. Run the same benchmark cases against the new revision, including
   `scripts/benchmark.py --set verdict-quality` for the invented Turkish relevance
   boundaries. Use `scripts/benchmark_compare.py` on the before/after reports,
   selecting workflow IDs when needed, to inspect fixed/broke verdicts and
   relevance outcomes. Compare per-step evidence, model responses and reviewer
   reactions too. Retain previous runs; reactions do not silently tune prompts
   or outputs.

At startup, a changed source default creates a new default-origin version only if that exact default text has never been stored. It activates automatically only while a default-origin version is active. An active API version stays selected across source-default changes and restarts; operators must activate a new default explicitly if desired. Identical source defaults do nothing on restart. Backend source/rule changes still require restart and also change the fingerprint. Historical format 1 snapshots remain readable without a `prompts` field; format 1/2 snapshots remain readable without a `models` block.

The fingerprint identifies the configured implementation, not a guarantee of identical generative output or unchanged remote web pages. Returned model IDs and completion envelopes are stored per call. If model weights are replaced under the same server ID, assign a different `MODEL_NAME`/server model identifier; identical names do not prove identical weights. Remote source/search changes remain visible in per-run evidence.

## Provider profiles and step models

The default `lmstudio` provider preserves the existing request body, sampling controls, token limits and prompts byte-for-byte. Its default response mode is `json_schema` and its default thinking control is `lmstudio`. The `openai_compatible` provider defaults to `auto` response mode and `none` thinking control. LM Studio thinking control is invalid with that provider; explicit `reasoning_effort` sends only that extension. Provider credentials never enter public configuration, snapshots, transcripts or errors.

The five model-using steps (classification, relevance, related removals, source and web search) each have a descriptor sharing one OpenAI client. Classification's coarse and fine calls use the same step model. `MODEL_NAME_<STEP>` overrides the default; empty means use `MODEL_NAME`. Only the default name resolves `auto` through provider discovery, filtering embedding/reranking models. Similarity makes no model call and has no override. Each saved step records the requested model name when a call occurred, otherwise `null` (including skipped and synthetic steps). Returned model identities remain in individual call traces.

`MODEL_RESPONSE_FORMAT=json_schema` preserves the existing schema request. `json_object` sends `{"type":"json_object"}` and appends this fixed instruction to the system prompt, separated by two newlines: `Return only a JSON object that conforms to the following JSON schema:` followed by a newline and the result type's JSON schema (sorted keys). Pydantic output validation and non-finite-number rejection remain identical. String-only calls retain their existing request without a response format.

`auto` first tries `json_schema`. Only an SDK `BadRequestError`/`UnprocessableEntityError` with HTTP 400/422 triggers one retry of that call using `json_object`. The fallback is remembered by model name across runs for the lifetime of this single-process engine. Other errors and invalid model output never trigger this retry. Each attempt has its own trace, including the response mode actually used; rejected attempts retain only their exception type. Failure continues through the existing stage failure rules, including relevance failures ending as `AUDIT_FAILED`.

Format 3 definitions contain `models: {provider, response_format, default, steps}` with all five effective names. API assignments share the prompt activation lock and atomically save their workflow with the assignment. A run builds private agents from both its captured prompts and models. Returning to an earlier mapping restores its workflow ID when the rest of the definition matches. Assignment author/note/timestamp and API credentials do not affect the fingerprint. Existing installations receive a new workflow ID on restart because the format changes, even though defaults and verdict semantics are unchanged. Benchmark deliberate provider/model changes before relying on equivalence.

## Intentional rewrite differences

LangGraph replaces the sequential orchestrator; a small OpenAI-compatible structured JSON adapter replaces pydantic-ai. Prompts, textual thresholds and the central verdict rules were retained at the migration, except that the relevance prompt's example headlines were replaced with fabricated equivalents before publishing. The relevance prompt has since been rewritten around public affairs instead of wrongdoing; its example headlines are invented and deliberately cover different subjects from the benchmark set. Model transport/schema behavior can still affect predictions; benchmark before trusting equivalence. Failure descriptions and skip explanations can differ. Old comment rendering, publishing jobs and apply ledgers are not part of this service. Input `similar_articles` remains accepted for structural compatibility, but archive similarity lookup supplies the step-2 candidates, as in the archived implementation.
