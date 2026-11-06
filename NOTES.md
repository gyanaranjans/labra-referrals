# NOTES

Status: everything in the brief is implemented and covered by `uv run pytest` (26 tests). Nothing is stubbed.
The scale sections below are design only, as the brief asks.

## 1. Schema

Three tables (full DDL in README):

- `labra_fields(name PK, label, required, format, position)` — **the shared list lives here, once.**
- `mapping_templates(id PK, name, crm_type)`
- `template_field_mappings(template_id, labra_field, crm_object, crm_field, unused)`, PK `(template_id, labra_field)`,
  `UNIQUE(template_id, crm_object, crm_field)` — **Northwind's CRM mappings live here**, one row per field the
  customer mapped or hid.

A template is never stored as a full document. `GET` computes it as
`labra_fields LEFT JOIN template_field_mappings ON labra_field AND template_id = ?`. A Labra field without a row is
returned as `crm_object: null, crm_field: null, mapped: false`.

**Introducing `customer_duns`:** `POST /labra-fields` does one `INSERT ... ON CONFLICT DO UPDATE` into
`labra_fields`. That is the only write. Zero `mapping_templates` or `template_field_mappings` rows are written; the
next `GET` of Northwind picks the field up through the join, and Northwind's six mapping rows are byte-identical
(the test snapshots the table before/after). Calling it again updates label/required/format in that one row; since
the customer's `unused` flag and mapping are on the other table, keyed by field name, they survive a relabel.

**What I refuse to store:** a copy of the default field list (labels, required flags, the six unmapped defaults)
on each template. That copy is exactly what goes stale and what someone would otherwise open 10,000 rows to
paste `customer_duns` into, or write a one-off rewrite script for.

**One field list, not two.** The Pydantic `Referral` model is built per request with `create_model` from the same
`labra_fields` rows the template GET uses. Each row's `format` picks a type from a fixed registry
(`labra/referral.py:FORMATS`). The registry is the set of *validation kinds*, not a second field list: adding a
field never needs a code change unless it needs a new kind of rule. One honest compromise: `DEFAULT_FORMATS =
{"customer_duns": "duns"}` exists only so the brief's three-key request body yields 9-digit validation. In a real
system I'd make `format` part of the request and delete that dict.

## 2. 10,000 templates, one new AWS field

Cost of the write: one row, constant time, regardless of template count. In SQLite it holds the database write
lock for well under a millisecond; in Postgres it's a single-row lock on `labra_fields` and nobody waits. No
deploy is needed — it's an API call (or a seed migration with one `INSERT`). No template rows are locked or
rewritten, so there's no backfill, no long transaction, and no partial state where half the templates have the
field.

What it costs instead: every template read is a join against the field list (tens of rows — trivially cached),
and the "template" is not a self-contained document you can diff in isolation. If we ever need "what did this
template look like on date X", that needs field-list versioning (`valid_from/valid_to` on `labra_fields`), which I
did not build.

Edge case: making an existing field `required: true` also writes one row. Templates that never mapped it then
fail referral validation with "Map customer_duns to a Salesforce field on template …" — the correct prompt, not a
silent pass. A template that had hidden that field is validated anyway; the AWS requirement beats the old choice.

## 3. Several CRM objects

The fetch list comes from the same template view the customer sees: take fields where `mapped` is true (so
unmapped and `unused` fields drop out, including a newly introduced field until someone maps it), group by
`crm_object` preserving field order, and call `crm.fetch(object, record_id, [fields])` once per object. Ten fields
on `Opportunity` are one call with ten field names. A CRM field can't be listed twice for one template because of
`UNIQUE(template_id, crm_object, crm_field)`. Only requested keys reach validation, so `InternalNotes` never
leaves the client.

If an object returns `CrmRecordNotFound` the request is 404 naming the object. If an object is rate limited twice
the request ends with 503 and a single error naming the object; no fields from it are validated, so Amount and
CloseDate are not reported as missing.

## 4. Rate limits at scale

200 prepares/min × 3 objects = 600 calls/min = 100 per 10 s — right at a portal limit of 100/10 s, with zero
headroom, and limits are per CRM portal (per customer org), so one busy customer can exhaust their own budget.

- **Where:** a shared token bucket in Redis (atomic Lua script or `redis-cell`), consulted by every API process.
  It wraps the single seam that touches the CRM: `fetch_with_one_retry` in `labra/app.py`, i.e. around each
  `crm.fetch(...)` call (`FakeCrm.fetch` in tests). Acquire a token before each fetch.
- **Key:** `crm:{crm_type}:{portal_id}` (Salesforce org id / HubSpot portal id), capacity and refill set to just
  under that portal's published limit (e.g. 90/10 s). Salesforce also has a 24-h API quota per org; that's a
  second, slower bucket under the same key prefix.
- **No slot within a few seconds:** don't hold the HTTP request open. Return 503 + `Retry-After` with the same
  "rate limit held for X" error (as the code already does after one retry), or, better for the UI, accept the
  prepare as a job (202 + job id) and let a worker drain it when tokens are available.
- **429 from the CRM despite the limiter** (other integrations share the org's quota): honour `Retry-After`, retry
  once (implemented in `fetch_with_one_retry`), then push the bucket's next-available time forward so other
  processes back off too.

Implemented: exactly one retry. `fetch_with_one_retry` catches `RateLimited`, sleeps `retry_after_ms`, fetches
that object once more; a second `RateLimited` propagates and becomes the 503.

## 5. One thing left out: optimistic concurrency on template edits

Two people editing the same template today: last write wins per field (each `PUT .../fields/{labra_field}` only
touches its own row, so edits to *different* fields don't clobber each other, which already covers most cases).
For same-field conflicts I'd add `version INTEGER` to `mapping_templates`, return it as an `ETag` on GET, require
`If-Match` on the PUT, and do `UPDATE mapping_templates SET version = version + 1 WHERE id = ? AND version = ?`
in the same transaction as the mapping write; zero rows updated → 412 with a fix of "reload the template and
reapply your change". Introducing a Labra field doesn't bump template versions, since it doesn't edit templates.

## Other choices worth flagging

- Error text is per *format*, not per field, e.g. `Set Account.Website in Salesforce to a URL such as
  https://northwind.example.` A blank required value (e.g. `Account.Name = " "`) is reported as "required but
  empty" with the raw received value, rather than as a length error.
- `expected_monthly_revenue` is `int | float`, so `"5000"` → `5000` (int) and `"1200.50"` → `1200.5`.
- Emails use a small regex rather than `email-validator`, which rejects the reserved `.example` TLD used in the brief.
- The "mark every Contact field unused" test first makes `primary_sales_rep_email` optional through
  `POST /labra-fields`, because the brief also requires that a required field cannot be hidden.
- Rate-limit-twice is tested with a `FakeCrm` subclass, because `fail_next` uses a set and can't queue two failures.
