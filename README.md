# Labra referral mapping service

FastAPI + Pydantic v2 + SQLite (stdlib `sqlite3`, no server to install). Python 3.11+.

## Setup

```sh
uv sync                        # or: python -m venv .venv && . .venv/bin/activate && pip install -e . pytest httpx
uv run python -m labra.db      # creates ./labra.db and seeds the six Labra fields (idempotent)
```

`create_app()` also runs the same idempotent init, so the DB is created on first start if you skip the step above.
Set `LABRA_DB=/path/to/file.db` to use another file.

## Run

```sh
uv run uvicorn labra.main:app --port 8000
```

`labra/main.py` wires the app to a `FakeCrm` seeded with Northwind `opp-100` (Salesforce objects) and Contoso
`deal-200` (HubSpot objects). There are no real CRM calls anywhere.

## Test

```sh
uv run pytest
```

Tests use a temp SQLite file per test, inject `FakeCrm` through `create_app(crm, db_path, today)`, and freeze
"today" at 2026-11-04.

## Walkthrough

```sh
# 1. Create Northwind
curl -s -XPOST localhost:8000/mapping-templates -H 'content-type: application/json' -d '{
  "id": "northwind-default", "name": "Northwind default", "crm_type": "salesforce",
  "mappings": [
    {"labra_field": "company_name",             "crm_object": "Account",     "crm_field": "Name"},
    {"labra_field": "website",                  "crm_object": "Account",     "crm_field": "Website"},
    {"labra_field": "country",                  "crm_object": "Account",     "crm_field": "BillingCountry"},
    {"labra_field": "expected_monthly_revenue", "crm_object": "Opportunity", "crm_field": "Amount"},
    {"labra_field": "target_close_date",        "crm_object": "Opportunity", "crm_field": "CloseDate"},
    {"labra_field": "primary_sales_rep_email",  "crm_object": "Contact",     "crm_field": "Email"}
  ]}'

# 2. Introduce customer_duns (AWS added a field)
curl -s -XPOST localhost:8000/labra-fields -H 'content-type: application/json' \
  -d '{"labra_field": "customer_duns", "label": "Customer DUNS", "required": false}'

# 3. The template saved before step 2 now shows customer_duns, unmapped; its mappings are untouched
curl -s localhost:8000/mapping-templates/northwind-default

# 4. Build the referral from the CRM
curl -s -XPOST localhost:8000/mapping-templates/northwind-default/crm-field-values \
  -H 'content-type: application/json' -d '{"record_id": "opp-100"}'
```

## API

| Method & path | Body | Notes |
|---|---|---|
| `GET /labra-fields` | | Shared field list, in display order. |
| `POST /labra-fields` | `{"labra_field", "label", "required"?=false, "format"?}` | Upsert. Introduces a field or updates its definition (label, required, format). Never touches templates. `format` is one of `text, url, country, positive_number, future_date, email, duns`; omitted → existing format, else `duns` for `customer_duns`, else `text`. |
| `POST /mapping-templates` | `{"id"?, "name", "crm_type", "mappings": [{"labra_field", "crm_object", "crm_field", "unused"?}]}` | 201 with the template view. `id` defaults to a slug of `name`. 422 (nothing stored) on any rule in the brief; 409 on duplicate id. |
| `GET /mapping-templates/{id}` | | Every current Labra field, with `crm_object`, `crm_field`, `mapped`, `unused`, `label`, `required`. |
| `PUT /mapping-templates/{id}/fields/{labra_field}` | `{"crm_object", "crm_field"}` to map, `{"unused": true}` to hide, `{}` to unmap | One customer choice for one field. Hiding or unmapping a required field → 422 ("required by AWS"). |
| `POST /mapping-templates/{id}/crm-field-values` | `{"record_id"}` | 200 `{referral, errors: []}`; 422 invalid CRM data; 404 unknown template or record; 503 CRM rate limit held after one retry. |

Every error is `{"field", "crm_object", "crm_field", "received", "message", "fix"}` inside `"errors": [...]`.
Template/field-list errors may have null `crm_object`/`crm_field`.

## Schema (SQLite)

```sql
CREATE TABLE labra_fields (            -- the shared list, owned by Labra/AWS
    name        TEXT PRIMARY KEY,
    label       TEXT NOT NULL,
    required    INTEGER NOT NULL DEFAULT 0,
    format      TEXT NOT NULL DEFAULT 'text',   -- picks the Pydantic type + error text
    position    INTEGER NOT NULL,
    updated_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE mapping_templates (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    crm_type    TEXT NOT NULL CHECK (crm_type IN ('salesforce', 'hubspot')),
    created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE template_field_mappings ( -- only the customer's choices (delta), never the defaults
    template_id  TEXT NOT NULL REFERENCES mapping_templates(id) ON DELETE CASCADE,
    labra_field  TEXT NOT NULL REFERENCES labra_fields(name),
    crm_object   TEXT,
    crm_field    TEXT,
    unused       INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (template_id, labra_field),
    UNIQUE (template_id, crm_object, crm_field),
    CHECK (unused = 1 OR (crm_object IS NOT NULL AND crm_field IS NOT NULL))
);
```

A template as the customer sees it = `labra_fields LEFT JOIN template_field_mappings` for that template.
No row → unmapped. Full source in `labra/db.py`.

## Layout

```
labra/db.py        schema, seed, connection helper
labra/crm.py       FakeCrm, RateLimited, CrmRecordNotFound, CrmClient protocol
labra/referral.py  formats → Pydantic model built from labra_fields, error translation
labra/app.py       create_app(crm, db_path, today) and routes
labra/main.py      uvicorn entrypoint with a seeded FakeCrm
tests/             pytest suite
```
