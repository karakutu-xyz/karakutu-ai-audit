# Audit result and record v2

Callers receive a lean **audit result**. A finished live run with a stored result can
also become an **audit record** for karakutu: the same shape, with a non-null record ID
and lineage. No route posts records yet; posting identity, endpoint and retries remain
open. Nothing edits archive articles, tags, audit status or decisions.

The machine-readable record contract is
[`audit-record.v2.schema.json`](audit-record.v2.schema.json), draft 2020-12,
`$id` `urn:karakutu-ai-audit:audit-record:2`. The `AuditResult` models and builders in
`backend/audit/record.py` are the source of truth. Generate the record schema with
`uv run python scripts/export_record_schema.py`; `--check` verifies it is current.
The API's nullable result contract is also included in `openapi.json`.

## Shape

Every object rejects unknown fields, and numbers cannot be infinite or NaN. All listed
fields are required; nullable fields explicitly carry JSON null when unavailable.

| Field | Contents |
| --- | --- |
| `schema` | Always `karakutu-ai-audit/audit-result`. |
| `schema_version` | Always `"2.0"`. |
| `record_id` | `run_<32 hex>.r<revision>` for a record; null otherwise. |
| `lineage` | Initial, re-audit or correction relationship; null without a record. |
| `article` | Local `ref`, nullable `archive_id`, original `url`, and `content_sha256` of the exact submitted text before redaction. No title or text. |
| `run` | `id`, `origin`, `category`, `status`, `created_at`, nullable `finished_at`, and `workflow_id`. Times are RFC 3339 with an explicit offset. |
| `verdict` | Stored verdict, or null until an output is stored. |
| `flags` | Stored `AuditFlag` values, or an empty list. |
| `findings` | Stored findings, capped at 2,000 characters, or null. |
| `classification` | Stored `subject_tags` (notation and label), `genre`, `geographic_scope`, and `gist` capped at 300 characters; null when absent. |
| `stages` | Six summaries in pipeline order: `stage`, `outcome`, `summary`. Each summary is the stored explanation with whitespace collapsed, capped at 300 characters. Empty without output. |
| `evidence` | Typed facts from stored output stage `details`, described below; null without output. |
| `error` | Stored fixed `audit_failed` message, or null. Cancellation and interruption are lifecycle states, not audit errors in this contract. |
| `notice` | The fixed machine-triage notice, unchanged. No verdict, including `POSSIBLE_CENSORSHIP`, is a finding of censorship; a person decides on independent evidence. |

Benchmark, queued, running, cancelled and interrupted runs produce **no record**.
Their API result has `record_id: null` and `lineage: null`. A completed benchmark can
still have a verdict and summaries in its API result. Seeded demo rows use
descriptive fixture IDs rather than portable run IDs and also have null record identity. An execution failure with a stored
`audit_failed` error can be a record even without an output; its verdict remains null.

### Evidence and limits

Evidence comes only from the stored output, never step evidence or upstream responses.
Missing `details` and missing keys in older rows become null or empty lists.

| Evidence field | Source |
| --- | --- |
| `relocated_to`, `old_in_new`, `new_in_old` | Web-search relocation details, falling back to similarity details when web search has no relocation. |
| `covers_key_facts` | Web-search `covers_key_facts`, or null if no comparison was stored. |
| `original_url_status` | Source `self_status`: `live_intact`, `live_other`, `dead_or_unreachable`, `not_probed`, or null. |
| `live_copies` | Web-search `matches`. |
| `dead_copies` | Web-search `dead_same_story`, reduced to `url` and `reason`. |
| `related_same_publisher` | Web-search `related_same_publisher`. |
| `search_queries`, `search_queries_failed` | Stored web-search counts, or null. |

Each evidence list keeps at most five eligible items. Evidence URLs longer than 512
bytes (UTF-8) are skipped, including the singular relocation URL; URLs are never cut into
misleading links. Every text cap counts UTF-8 bytes, not characters: findings 2,000, each
stage summary 300, the gist 300, each tag notation and label 120, each dead-copy reason 120.
Classification retains at most five subject tags. A cut text ends with `…`.

A typical record is a few KB. The caps bound every record: the article URL is at most 8 KB
(a longer one refuses the record with `too_large`), and everything else together stays well
under the hard limit `RESULT_MAX_BYTES` of **32,768 bytes**. `build_record` still checks the
final UTF-8 JSON and refuses an oversized record with `too_large`, as a fail-closed backstop.

## What stays on this service

Article text, similar articles, fetched page text, exact step input/output, prompts and
model calls remain in private storage behind the explicit
`GET /api/runs/{id}/details` review route. The explicit full download remains
`GET /api/runs/{id}/export`. Both retain the service's existing authentication rules.
They are full review data, not the portable record.

The result has no request echo, step dumps, confidence values, provenance dump, prompt
hashes, model-call dumps, producer timestamp or redactions list. The stable
`run.workflow_id` points to the full preserved snapshot at `GET /api/workflows/{id}`.
Keeping one pointer avoids duplicating implementation and prompt data in every result.

## Redaction and refusals

`build_result(run, ...)` builds API results for stored runs in every category and state,
validates the shared model, and returns plain JSON. It scrubs credential-key values and
credentials inside URLs. `build_record(run, workflow, ...)` additionally checks record
eligibility, workflow identity, timestamps and lineage, then scans the projected result
for credentials and configured secrets before returning it.

The record refusal codes are `not_live`, `not_finished`, `no_output`,
`workflow_mismatch`, `invalid_timestamp`, `invalid_lineage`, `credential_detected`,
`secret_detected`, and `too_large`. Interrupted runs return `not_finished`. A terminal
run without output or a stored audit-failure error returns `no_output`.

Credential-like keys (`authorization`, `cookie`, `api_key`, `token`, `wallet`, `jwk`,
`traceback`, and similar exact names) have their values replaced with `[redacted]`.
In every string, absolute and scheme-relative URLs lose userinfo and the values of
credential query or fragment parameters such as `access_token`, `key` and `sig`.
A parameter containing an encoded credential URL is redacted as a whole. Semicolons
and HTML-escaped separators are covered. Other URL bytes are retained.

The final credential scan covers every string, including keys, through every state
reachable by up to five single URL-decoding or HTML-unescaping steps, with and without
`+` as space. Remaining credentials or still-expanding encodings refuse the record.
Only an exact `[redacted]` value counts as redacted. Supplied configured secret values,
of any nonzero length, appearing anywhere in any decoding state refuse the record with
`secret_detected`; the final serialized JSON is checked too.

### What the guards promise

- **Fail closed for supplied secrets:** the future poster must pass every configured
  model/API key, bearer token and wallet secret to `build_record`. Any surviving value
  in the portable result refuses it. The API result builder performs redaction but does
  not have that poster-supplied secret inventory or the record's final refusal scans.
- **No raw diagnostics or transcripts by projection:** the portable result copies only
  the named stored result fields and fixed service errors. Article text, upstream bodies,
  prompts and raw model completions have no field in this contract. Findings and summaries
  remain model-derived audit prose, subject to caps and the credential/secret scans.
- **Best effort for unknown third-party credentials:** URL scrubbing and the final
  credential scan catch common and encoded forms and refuse when decoding stays ambiguous.
  They do not recognize every possible spelling or browser interpretation. A URL split by
  a tab or line break, for example, is not recognized as one URL.

## Lineage and storage keys

The article is filed under `article.archive_id`, or `article.url` when that is unknown.
`article.ref` links to this service's local article history. The record ID is the future
post's idempotency key: repeating the same ID should keep one copy; a different ID is a
new record. Serialization does not change historical run input or workflow snapshots.

| Kind | Revision | Relationship |
| --- | --- | --- |
| `initial` | 0 | No earlier eligible live record for this article. |
| `re_audit` | 0 | `previous_record_id` points to a different run's record. API reads select the latest finished live run with a stored result created strictly before this run, for the same `article_ref`, and point to its `.r0`. |
| `correction` | 1 or greater | Same run; points to its immediately preceding revision, with reason `record_error` or `redaction`. |

Runs without an article reference have initial lineage. The API always uses revision 0;
corrections are an explicit serializer operation. Invalid revision/relationship pairs
refuse with `invalid_lineage`. A correction supersedes an earlier record; it cannot erase
bytes already held in permanent storage.

## Versioning

Version 2 is a new major contract. Version 1 was never posted and is removed outright;
there is no compatibility serializer or parallel old schema. A future major change that
removes or changes fields needs a new schema file and `$id`. A minor change can add
optional fields within the major version; consumers validating closed objects must
update their schema before accepting new fields. Karakutu should retain each record's
`schema_version` and store its bytes unchanged.
