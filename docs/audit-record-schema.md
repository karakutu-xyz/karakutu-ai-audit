# Audit record schema v1

When an audit finishes, this service will post one **audit record** to karakutu. karakutu stores it
exactly as received. This page is the contract for that record. The machine-readable form is the
JSON Schema in [`audit-record.v1.schema.json`](audit-record.v1.schema.json) (draft 2020-12,
`$id` `urn:karakutu-ai-audit:audit-record:1`).

The record is the only thing the service writes to karakutu (a v1 design decision). It never adds
discussion comments, edits tags, or changes an audit's status or decision. A record is a machine
triage result: no verdict, including `POSSIBLE_CENSORSHIP`, is a finding of censorship. A person
decides, on independent evidence. Every record carries that sentence in `notice`.

This page defines the record only. A later change will post records (endpoint, identity,
retries); the karakutu side of the endpoint agreement is still open.

## Source of truth

- Models: `backend/audit/record.py` (`AuditRecordV1`). The JSON Schema is generated from them
  with `uv run python scripts/export_record_schema.py`; `--check` and a test fail when the
  committed file is stale.
- Serializer: `build_record(run, workflow, ...)` turns a stored run (`Store.run`) and its workflow
  snapshot (`Store.workflow`) into a record, validates it, and returns plain JSON.
- Test: `backend/tests/test_audit_record.py` drives a real run through the engine and the store
  with fabricated data, serializes it and validates it against the committed schema file.

## Shape

| Field | Contents |
| --- | --- |
| `schema` | Always `karakutu-ai-audit/audit-record`. |
| `schema_version` | `"1.0"` for this version. See [Versioning](#versioning). |
| `record_id` | `<run id>.r<revision>`, for example `run_<32 hex>.r0`. Unique per record. |
| `lineage` | How this record relates to earlier ones. See [Re-audits and corrections](#re-audits-and-corrections). |
| `article` | Reference to the audited article: `ref` (this service's article reference, `ka-<archive id>` or `u-<url hash>`), `archive_id` (karakutu's own article id, as a string), `url`, `title`, `published_at`, and `content_sha256` of the submitted text. Every run stored by this service has a `ref` (a URL-only submission gets `u-<url hash>`); `archive_id` is null when karakutu's id is not known. |
| `request` | The submitted input as stored: `removed_article` (url, title, content, published_at) and up to five `similar_articles`. |
| `run` | Run `id`, `origin` (`api`, `webhook`, or `article` for a first run or re-run started from an article), `category` (always `live`), `status` (`succeeded` or `failed`), `synthetic` (true when the model was disabled), `created_at`, `finished_at` and `duration_ms` (submission to finish, including time in the queue). |
| `result` | The audit response as stored: `verdict`, `findings`, `flags`, `short_circuited_at`, `classification`, and the six stage results. |
| `error` | Null, or `{code: "audit_failed", message}` when the audit could not complete. |
| `steps` | Exactly six step records in pipeline order (classification, similarity, relevance, related_removals, source, web_search). Each has `name`, `status` (`completed`, `failed`, `skipped`), `model`, `duration_ms`, `input`, `output`, `model_calls` and `evidence`. |
| `provenance` | The workflow that ran: `workflow_id`, `workflow_created_at`, `format_version`, per-step `models`, `prompts` (template text and SHA-256 for each prompt), step names, the decision rule, safe `settings`, a digest of the backend `sources` (file count and SHA-256) and a SHA-256 per locked dependency file. |
| `model` | The run's model identity (`name`, `enabled`). |
| `producer` | `service` (`karakutu-ai-audit`) and `serialized_at`. |
| `redactions` | JSON pointers of values the redaction pass replaced. Usually empty. |
| `notice` | The fixed "not a finding of censorship" sentence. |

Step `input`, `output` and `evidence` entries are open JSON objects: their keys follow each
step's code (`backend/audit/workflow/<step>.py`) and may grow without a schema change. The
typed guarantees live in `result`. Timestamps the service writes (`run`, `provenance`,
`producer`) are RFC 3339 with an explicit offset; the serializer refuses anything else
(`invalid_timestamp` for run times). `published_at` is kept as submitted (ISO 8601, the offset may be absent).

### What a step carries

- `input`: what the step was given: the article, the prior stage results, and the earlier steps'
  evidence objects it depends on. Classification's IPTC catalog is karakutu's own data and the same
  for every run, so the record replaces it with `{omitted: "catalog", sha256, topics, genres}`.
- `output`: the stage result (`outcome`, `confidence`, `explanation`, `skipped`, `details`).
- `evidence`: every search, archive read, page fetch and liveness probe the step made, with what
  came back. Failures carry only an `error_type` (an exception class name), never its text. An
  archive read keeps its path, parameters and status, but its raw karakutu response body becomes
  `{omitted: "archive_response", sha256}`: karakutu holds that data, and an upstream body may carry
  diagnostics. What the step used from it is in the step's `output` and later steps' `input`.
- `model_calls`: one summary per model call: `requested_model`, `response_model`,
  `response_format`, `temperature`, `max_tokens`, the model's validated structured answer
  (`parsed_output`; null for a free-text answer such as the related-removals selection, whose
  grounded result is in the step output), token counts (`usage`: prompt, completion and total
  tokens only), `error_type`, and SHA-256 hashes of the system prompt, the user prompt, the whole request and
  the provider's raw completion (`completion_sha256`, null when no completion arrived).

## Model exchanges: what goes in

Decision for v1: the record carries prompt **templates** and the model's **parsed answers**, not
the filled exchanges.

- In: each prompt template's full text and hash (in `provenance.prompts`), each call's validated
  structured answer and token counts, and hashes of the exact system prompt, user prompt, request and raw completion.
- Out: the filled `messages` and the provider's raw completion.

Why: the filled user prompt is the article and the evidence again, which the record already holds,
so it would roughly double the record for no new information. The raw completion adds only
provider metadata. The project rule that exact transcripts stay in local storage and
authenticated review responses still holds. The hashes let anyone holding the local run
(`GET /api/runs/{id}`) prove that the record matches the exchange that happened, both what was
sent and what came back.

## Excluded

- Secrets: API and model keys, bearer tokens, cookies, wallet keyfiles and any wallet material.
  The workflow settings in `provenance` are the safe subset already used for fingerprints; endpoint
  URLs have no credentials, query or fragment.
- Raw exception text and tracebacks. Errors are fixed messages and exception class names only.
- Reviewer reactions and notes, run labels and expected verdicts: those are local review data,
  and the record must not read as a comment or a decision.
- Backend source and lock file texts (only their hashes; the full snapshot stays available at
  `GET /api/workflows/{id}`).
- Benchmark runs. The serializer refuses any run whose category is not `live`, and any run that
  has not finished or has no stored result (for example a run interrupted by a restart).

A redaction pass replaces the value of any key named like
a credential or a traceback (`authorization`, `cookie`, `api_key`, `token`, `wallet`, `jwk`,
`traceback`, and similar exact names) with `"[redacted]"` and lists its JSON pointer in
`redactions`. Inside step `evidence`, which holds upstream response bodies verbatim, it also
redacts error-text keys (`error`, `errors`, `message`, `detail`, `stack` and similar); the
service's own evidence events record only `error_type`. In every string, URLs (absolute or
scheme-relative `//host`) lose their
userinfo (`user:password@`) and the values of credential-like query or fragment parameters
(`access_token`, `key`, `sig`, `X-Amz-Signature` and the names above); the rest of the URL is
kept byte for byte. A parameter whose value, once decoded, holds such a credential is blanked as
a whole. `;` counts as a separator, so HTML-escaped links (`&amp;access_token=`) are covered.
`article.content_sha256` is computed from the submitted text before redaction.

A final pass then decodes every string, prompt templates included, through every state reachable
by up to five single URL-decoding or HTML-unescaping steps. If any string still holds URL userinfo or a credential parameter with a value, or
still yields new states after five steps, serialization stops with `credential_detected`. Only a value that
is exactly `[redacted]` counts as redacted. Prompt
templates are never edited, so a credential in a template always refuses the record.

The poster also passes its configured secret values; if any of them (of any length) appears
anywhere in the record, in any state reachable by up to five single
URL-decoding or HTML-unescaping steps (with and without `+` as a space), serialization stops with `secret_detected` and nothing is sent.

### What the guards promise

Decided for v1 (2026-11-20):

- **Guaranteed, fail closed:** the service's own secrets never leave in a record. The poster passes
  every configured secret (model and API keys, bearer tokens, wallet material), and any of them in
  any decoding state refuses the record. The service never puts these into run data in the first
  place: settings are the safe fingerprint subset and endpoints carry no credentials.
- **Guaranteed:** no raw exception or diagnostic text from the service, the model provider or
  karakutu. The service records exception class names only; provider usage is reduced to token
  counts; raw karakutu bodies become digests; free-text model answers are not copied. The model's
  own structured answers (reasoning, explanations, the findings built from them) are the audit
  itself and go in as written; whatever text a model writes there is model output, not a system
  diagnostic.
- **Best effort:** credentials that third parties wrote into public web text (the submitted
  article, fetched pages, search snippets). That text is already public, and karakutu archives the
  same pages. The URL scrubbing and the final credential pass catch the common and encoded forms
  and refuse the record when unsure, but they do not parse text the way every browser would: for
  example, a URL split by a tab or a line break is not recognised as one.

## Idempotency key and storage key

- **Article reference.** karakutu files every record under the article it is about:
  `article.archive_id` (its own article id), or `article.url` when that is null. All records for
  one article form its audit history. `article.ref` is this service's reference for the same
  article, so a record can be traced back to its local article history.
- **Record id.** `record_id` is the idempotency key of a post. The same record posted twice (a
  retry after a timeout) carries the same `record_id`; karakutu should accept the repeat and keep
  one copy. A different `record_id` is always a new record.

## Re-audits and corrections

karakutu storage may be permanent, so a record is never edited or deleted. Changes are new records
that point back.

| `lineage.kind` | When | `record_id` | Points to |
| --- | --- | --- | --- |
| `initial` | First record of a run. | `<run>.r0` | nothing |
| `re_audit` | A new run audited the same article again (new evidence, new workflow). | `<new run>.r0` | `previous_record_id`: the latest record for that article, from an earlier run. |
| `correction` | The same run was serialized again because the earlier record was wrong (`reason: "record_error"`) or exposed something it should not have (`reason: "redaction"`). | `<run>.r<n+1>` | `previous_record_id`: `<run>.r<n>`. |

A reader takes the newest record in the chain as current and keeps the older ones as history. A
`redaction` correction cannot remove data from permanent storage; it marks the earlier record as
superseded so karakutu can hide it where its storage allows. The serializer checks these rules and
refuses inconsistent lineage with `invalid_lineage`.

## Versioning

- `schema_version` is `"<major>.<minor>"`. A major version renames, removes or changes the
  meaning of a field; it gets a new schema file (`audit-record.v2.schema.json`) and a new `$id`.
- A minor version only adds optional fields, and its schema replaces
  `audit-record.v1.schema.json`. A newer 1.x schema accepts every older 1.x record. An older
  schema rejects a newer record's new fields, because fixed objects reject unknown fields, so a
  consumer that validates must update the schema before accepting a newer minor version.
- The open objects inside steps (`input`, `output`, `evidence`) may gain keys at any time without a
  version change.
- karakutu stores the record bytes as received and keeps `schema_version` with it.

## Size

Records are self-contained, so the article text appears in `request` and again in each step's
`input`, and fetched pages appear in `evidence`. A typical record is tens of kilobytes; the input
limits (250,000 characters of article text, five similar articles) bound the worst case at a few
megabytes. The maximum size karakutu accepts is part of the karakutu endpoint agreement (still open).

## Open with the karakutu team

- The endpoint that accepts and stores records unchanged, keyed by article reference, and
  idempotent on `record_id`.
- The identity it accepts: one that can only post records, never change audit status or decision.
- Who can read stored records, and whether they land on Arweave (permanent).
- The maximum record size.
