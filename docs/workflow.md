# Workflow and iteration

`backend/audit/engine.py` builds a LangGraph `StateGraph` with six named steps and conditional edges. `workflow/` owns domain logic and prompts; `search/` owns measured text matching and guarded page fetching. The original evidence logic is retained instead of replacing calibrated rules with model opinion.

| Step | Input and action | Output / branch |
| --- | --- | --- |
| 1. Classification | Article text plus catalog-grounded IPTC topic/genre menus. Coarse then fine model calls. | Up to five validated tags, genre, scope and gist. Unavailable catalog or failed classification never decides the audit. |
| 2. Similarity | Archive similarity candidates, fetched through the public-address guard. | Measured same-publisher republication ends as `MOVED` when at least 99% of the original is contained. Remaining steps are recorded as skipped. |
| 3. Relevance | Title and bounded article text. | Public-interest judgment. A failed model call is `AUDIT_FAILED`; a low-interest judgment waits for step 4 before exiting. |
| 4. Related removals | Archive removal window (±14 days), same-subject model matching. | At least three confirmed related items across at least two publishers, or a related item already marked censored by a human auditor, raise `RELATED_REMOVALS`, keeping an otherwise low-interest item in scope. Missing archive evidence does not manufacture a pattern. Strong key terms are lone acronyms (not all-caps header runs or labels) and two-word names (not company or legal-form pairs such as "Anonim Şirketi"); a name matches only from a word start. |
| 5. Source | Original URL, article citations, source search. | A live original with measured text overlap ends as `NOT_REMOVED`. A cited-source 404/410 may supply `SOURCE_DEAD`; access blocks and fetch refusals cannot. A live cited page counts as the same story, and becomes a web-search lead, only when it holds at least 20% of the archived text (the sibling line); below that it is reported as not shown to be the same story. |
| 6. Web search | Search queries, archive/source leads, fetched page evidence and structured model outputs. | Models identify candidate matches; measured grounding and the deterministic decision function select the verdict. All failed model attempts mean `AUDIT_FAILED`. |

`workflow/pipeline.py::_decide` maps substantive evidence to `NOT_REMOVED`, `MOVED`, `LIKELY_MOVED`, `FOUND_ONLINE`, `POSSIBLE_CENSORSHIP`, or `INCONCLUSIVE`. The engine handles the explicit low-interest and failed-execution exits. The eight verdict names remain compatible with the archived auditor. Every `POSSIBLE_CENSORSHIP` explanation says it is not a finding of censorship and requires human review. A missing search hit is not evidence of censorship.

## Review a saved run

Open a run from the Runs ledger (`/runs/<id>`). The page puts the saved prediction and its findings first, then the run facts, the six workflow steps and an append-only human reaction (**Human review**). Reviewer identity is required; the note and expected verdict are optional.

Each step row opens to its recorded explanation, the exact input and output JSON, and **Evidence** and **Model calls** (prompts, model responses and fetch/search/archive evidence). **Article and records** shows the submitted text, with the exact run input and output on demand. **Saved workflow** opens the run's preserved definition, not the currently configured workflow. These are presentation changes only; stored runs and verdict rules are unchanged.

## Run lifecycle and operator controls

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

Each completed/skipped step stores its input state, output, duration, model calls and evidence events. A model call includes the actual system/user messages, response schema, sampling controls and token limit, the unmodified successful completion envelope, parsed result, returned model name and usage. Failed parsing retains the raw completion; transport errors retain an exception type only, never an upstream error body. Authentication headers and wallet material are excluded.

Search events retain query, provider results and failure type. Fetch events retain requested candidates and fetched/parsed evidence, including measured text. Archive events retain the GET path, query, status and decoded response. These private records exist for human review in the ignored local database. They are not log output. Network/SDK diagnostic logs are redacted; LangSmith tracing is explicitly disabled.

The run's input is the submitted audit request. Each step's actual bounded model input is also retained: this makes truncation and retry ladders visible. Evidence captures the text used by the workflow, not byte-for-byte copies of all HTTP wire traffic. Error response bodies and secrets are deliberately excluded.

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

LangGraph replaces the sequential orchestrator; a small OpenAI-compatible structured JSON adapter replaces pydantic-ai. Prompts, textual thresholds and the central verdict rules are retained, except that the relevance prompt's example headlines were replaced with fabricated equivalents (same category, language and boundary case) before publishing. Model transport/schema behavior can still affect predictions; benchmark before trusting equivalence. Failure descriptions and skip explanations can differ. Old comment rendering, publishing jobs and apply ledgers are not part of this service. Input `similar_articles` remains accepted for structural compatibility, but archive similarity lookup supplies the step-2 candidates, as in the archived implementation.
