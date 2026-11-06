import os
import re
import time
from datetime import date, datetime, timezone
from typing import Callable

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from labra.crm import CrmClient, CrmRecordNotFound, RateLimited
from labra.db import connect, init_db
from labra.referral import CRM_NAMES, DEFAULT_FORMATS, FORMATS, build_referral, error, is_blank


class LabraFieldIn(BaseModel):
    labra_field: str
    label: str
    required: bool = False
    format: str | None = None


class MappingIn(BaseModel):
    labra_field: str
    crm_object: str | None = None
    crm_field: str | None = None
    unused: bool = False


class TemplateIn(BaseModel):
    id: str | None = None
    name: str = ""
    crm_type: str = ""
    mappings: list[MappingIn] = []


class FieldChoiceIn(BaseModel):
    crm_object: str | None = None
    crm_field: str | None = None
    unused: bool = False


class CrmFieldValuesIn(BaseModel):
    record_id: str


def utc_today() -> date:
    return datetime.now(timezone.utc).date()


def fail(status: int, errors: list[dict], **extra) -> JSONResponse:
    return JSONResponse({**extra, "errors": errors}, status_code=status)


def general(message: str, fix: str) -> dict:
    return error(None, None, None, None, message, fix)


def load_fields(conn) -> dict:
    rows = conn.execute("SELECT * FROM labra_fields ORDER BY position").fetchall()
    return {
        r["name"]: {"labra_field": r["name"], "label": r["label"], "required": bool(r["required"]),
                    "format": r["format"], "position": r["position"], "updated_at": r["updated_at"]}
        for r in rows
    }


def template_view(conn, template_id: str) -> dict | None:
    t = conn.execute("SELECT * FROM mapping_templates WHERE id = ?", (template_id,)).fetchone()
    if t is None:
        return None
    rows = conn.execute(
        """
        SELECT f.name, f.label, f.required, f.format, m.crm_object, m.crm_field, COALESCE(m.unused, 0) AS unused
        FROM labra_fields f
        LEFT JOIN template_field_mappings m ON m.labra_field = f.name AND m.template_id = ?
        ORDER BY f.position
        """,
        (template_id,),
    ).fetchall()
    fields = []
    for r in rows:
        mapped = not r["unused"] and r["crm_object"] is not None
        fields.append({
            "labra_field": r["name"],
            "label": r["label"],
            "required": bool(r["required"]),
            "format": r["format"],
            "crm_object": r["crm_object"] if mapped else None,
            "crm_field": r["crm_field"] if mapped else None,
            "mapped": mapped,
            "unused": bool(r["unused"]),
        })
    return {"id": t["id"], "name": t["name"], "crm_type": t["crm_type"], "fields": fields}


def create_app(
    crm: CrmClient,
    db_path: str | None = None,
    today: Callable[[], date] = utc_today,
) -> FastAPI:
    db_path = db_path or os.environ.get("LABRA_DB", "labra.db")
    init_db(db_path)
    app = FastAPI(title="Labra referral mapping")

    @app.exception_handler(RequestValidationError)
    async def bad_request(_: Request, exc: RequestValidationError):
        return fail(422, [
            general(f"Request body is invalid at {'.'.join(map(str, e['loc']))}: {e['msg']}.",
                    "Fix that part of the JSON body and send it again.")
            for e in exc.errors()
        ])

    @app.get("/labra-fields")
    def list_fields():
        with connect(db_path) as conn:
            return list(load_fields(conn).values())

    @app.post("/labra-fields")
    def upsert_field(body: LabraFieldIn):
        errors = []
        if not re.fullmatch(r"[a-z][a-z0-9_]*", body.labra_field) or body.labra_field.startswith("model_"):
            errors.append(general("labra_field must be snake_case.",
                                  "Send a lowercase name such as customer_duns."))
        if is_blank(body.label):
            errors.append(general("label is blank.", "Send a non-empty label."))
        if body.format is not None and body.format not in FORMATS:
            errors.append(general(f"format {body.format!r} is unknown.",
                                  f"Send one of {', '.join(FORMATS)} or omit format."))
        if errors:
            return fail(422, errors)
        with connect(db_path) as conn:
            existing = load_fields(conn).get(body.labra_field)
            fmt = body.format or (existing["format"] if existing else DEFAULT_FORMATS.get(body.labra_field, "text"))
            # One row in labra_fields. No template row is touched: templates read this table on every GET.
            conn.execute(
                """
                INSERT INTO labra_fields (name, label, required, format, position)
                VALUES (?, ?, ?, ?, (SELECT COALESCE(MAX(position), -1) + 1 FROM labra_fields))
                ON CONFLICT (name) DO UPDATE SET
                    label = excluded.label, required = excluded.required,
                    format = excluded.format, updated_at = CURRENT_TIMESTAMP
                """,
                (body.labra_field, body.label.strip(), int(body.required), fmt),
            )
            return load_fields(conn)[body.labra_field]

    @app.post("/mapping-templates", status_code=201)
    def create_template(body: TemplateIn):
        with connect(db_path) as conn:
            fields = load_fields(conn)
            errors = []
            if is_blank(body.name):
                errors.append(general("Template name is blank.", "Send a non-empty template name."))
            if body.crm_type not in CRM_NAMES:
                errors.append(general(f"crm_type {body.crm_type!r} is not supported.", "Send salesforce or hubspot."))

            seen_fields, seen_pairs = set(), {}
            for m in body.mappings:
                f = fields.get(m.labra_field)
                if f is None:
                    errors.append(error(m.labra_field, m.crm_object, m.crm_field, None,
                                        f"{m.labra_field} is not a Labra referral field.",
                                        "Remove that mapping or add the field first."))
                    continue
                if m.labra_field in seen_fields:
                    errors.append(error(m.labra_field, m.crm_object, m.crm_field, None,
                                        f"{m.labra_field} is mapped more than once.",
                                        "Keep one CRM object and field per Labra field."))
                    continue
                seen_fields.add(m.labra_field)
                if m.unused:
                    if f["required"]:
                        errors.append(error(m.labra_field, None, None, None,
                                            f"{f['label']} is required by AWS and cannot be marked unused.",
                                            f"Map {m.labra_field} to a CRM object and field."))
                    continue
                if is_blank(m.crm_object) or is_blank(m.crm_field):
                    errors.append(error(m.labra_field, m.crm_object, m.crm_field, None,
                                        f"{f['label']} mapping is missing its CRM object or field.",
                                        "Fill both, or mark the field unused."))
                    continue
                pair = (m.crm_object.strip(), m.crm_field.strip())
                if pair in seen_pairs:
                    errors.append(error(m.labra_field, *pair, None,
                                        f"{pair[0]}.{pair[1]} is already mapped to {seen_pairs[pair]}.",
                                        "Pick one Labra field for that CRM value."))
                    continue
                seen_pairs[pair] = m.labra_field

            for name, f in fields.items():
                if f["required"] and name not in seen_fields:
                    errors.append(error(name, None, None, None, f"{f['label']} is required and has no mapping.",
                                        "Map that field before saving."))

            template_id = body.id or re.sub(r"[^a-z0-9]+", "-", body.name.lower()).strip("-")
            if not errors and conn.execute("SELECT 1 FROM mapping_templates WHERE id = ?", (template_id,)).fetchone():
                return fail(409, [general(f"Template {template_id} already exists.", "Send a different id.")])
            if errors:
                return fail(422, errors)

            conn.execute("INSERT INTO mapping_templates (id, name, crm_type) VALUES (?, ?, ?)",
                         (template_id, body.name.strip(), body.crm_type))
            conn.executemany(
                "INSERT INTO template_field_mappings (template_id, labra_field, crm_object, crm_field, unused) "
                "VALUES (?, ?, ?, ?, ?)",
                [
                    (template_id, m.labra_field, None, None, 1) if m.unused
                    else (template_id, m.labra_field, m.crm_object.strip(), m.crm_field.strip(), 0)
                    for m in body.mappings
                ],
            )
            return template_view(conn, template_id)

    @app.get("/mapping-templates/{template_id}")
    def get_template(template_id: str):
        with connect(db_path) as conn:
            view = template_view(conn, template_id)
        if view is None:
            return fail(404, [general(f"Template {template_id} not found.", "Check the template id.")])
        return view

    @app.put("/mapping-templates/{template_id}/fields/{labra_field}")
    def set_field_choice(template_id: str, labra_field: str, body: FieldChoiceIn):
        with connect(db_path) as conn:
            view = template_view(conn, template_id)
            if view is None:
                return fail(404, [general(f"Template {template_id} not found.", "Check the template id.")])
            f = next((f for f in view["fields"] if f["labra_field"] == labra_field), None)
            if f is None:
                return fail(404, [general(f"{labra_field} is not a Labra referral field.",
                                          "Use a field from GET /labra-fields.")])

            if body.unused:
                if f["required"]:
                    return fail(422, [error(labra_field, f["crm_object"], f["crm_field"], None,
                                            f"{f['label']} is required by AWS and cannot be marked unused.",
                                            f"Keep {labra_field} mapped to a CRM object and field.")])
                row = (None, None, 1)
            elif is_blank(body.crm_object) and is_blank(body.crm_field):
                if f["required"]:
                    return fail(422, [error(labra_field, None, None, None, f"{f['label']} is required.",
                                            "Send crm_object and crm_field for this field.")])
                row = None
            elif is_blank(body.crm_object) or is_blank(body.crm_field):
                return fail(422, [error(labra_field, body.crm_object, body.crm_field, None,
                                        f"{f['label']} mapping is missing its CRM object or field.",
                                        "Fill both, or mark the field unused.")])
            else:
                pair = (body.crm_object.strip(), body.crm_field.strip())
                other = next((o for o in view["fields"] if o["labra_field"] != labra_field
                              and (o["crm_object"], o["crm_field"]) == pair), None)
                if other:
                    return fail(422, [error(labra_field, *pair, None,
                                            f"{pair[0]}.{pair[1]} is already mapped to {other['labra_field']}.",
                                            "Pick one Labra field for that CRM value.")])
                row = (*pair, 0)

            conn.execute("DELETE FROM template_field_mappings WHERE template_id = ? AND labra_field = ?",
                         (template_id, labra_field))
            if row:
                conn.execute(
                    "INSERT INTO template_field_mappings (template_id, labra_field, crm_object, crm_field, unused) "
                    "VALUES (?, ?, ?, ?, ?)", (template_id, labra_field, *row))
            return template_view(conn, template_id)

    @app.post("/mapping-templates/{template_id}/crm-field-values")
    def crm_field_values(template_id: str, body: CrmFieldValuesIn):
        with connect(db_path) as conn:
            view = template_view(conn, template_id)
        if view is None:
            return fail(404, [general(f"Template {template_id} not found.", "Check the template id.")], referral=None)
        crm_name = CRM_NAMES[view["crm_type"]]

        by_object: dict[str, list[str]] = {}
        for f in view["fields"]:
            if f["mapped"]:
                by_object.setdefault(f["crm_object"], []).append(f["crm_field"])

        values = {}
        for obj, crm_fields in by_object.items():
            try:
                row = fetch_with_one_retry(crm, obj, body.record_id, crm_fields)
            except RateLimited:
                return fail(503, [error(None, obj, None, None,
                                        f"The {crm_name} rate limit held for {obj} after one retry.",
                                        "Wait a few seconds and prepare the referral again.")], referral=None)
            except CrmRecordNotFound:
                return fail(404, [error(None, obj, None, body.record_id,
                                        f"{crm_name} has no {obj} record for {body.record_id}.",
                                        "Check the record_id, or that the record exists in the CRM.")],
                            referral=None)
            values.update({(obj, k): v for k, v in row.items()})

        referral, errors = build_referral(template_id, view["crm_type"], view["fields"], values, today)
        if errors:
            return fail(422, errors, referral=None)
        return {"referral": referral, "errors": []}

    return app


def fetch_with_one_retry(crm: CrmClient, obj: str, record_id: str, fields: list[str]) -> dict:
    try:
        return crm.fetch(obj, record_id, fields)
    except RateLimited as exc:
        time.sleep(exc.retry_after_ms / 1000)
        return crm.fetch(obj, record_id, fields)
