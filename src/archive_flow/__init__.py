"""The archive flow: one audit id → body recovered from the archive → six-step audit → IPTC tags
and one discussion comment written back under the operator's standing rules.

Moved from `scripts/open_audit/` (2026-10-05) so the batch CLIs and the article job worker run ONE
implementation. Every module is a relocation, not a rewrite; golden tests pin byte-identity
(`tests/test_archive_flow.py`).

  rules      ← scripts/open_audit/apply_rules.py          (verbatim)
  comments   ← scripts/open_audit/comment_templates.py    (verbatim except its import)
  intake     ← fetch_listed_cases.py::one                  (build_case)
  runner     ← run_open_cases.py case → record             (run_case, dedupe_cases, wait_for_model)
  applier    ← apply_results.py per-record body            (apply_record, classification_payload)

`scripts/open_audit/{apply_rules,comment_templates}.py` stay as re-export shims.
"""
