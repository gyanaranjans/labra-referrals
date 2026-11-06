"""Referral validation. The Pydantic model is built from the labra_fields rows, so the field list has one source."""

import re
from dataclasses import dataclass
from datetime import date
from typing import Annotated, Any, Callable, Literal
from urllib.parse import urlparse

from pydantic import (
    AfterValidator,
    BeforeValidator,
    Field,
    StringConstraints,
    ValidationError,
    ValidationInfo,
    create_model,
)

COUNTRIES = ("United States", "India", "Germany", "United Kingdom")
CRM_NAMES = {"salesforce": "Salesforce", "hubspot": "HubSpot"}


def _url(v: str) -> str:
    parsed = urlparse(v)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("not an http(s) URL")
    return v


def _iso_date_string(v: Any) -> Any:
    if isinstance(v, str) and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
        raise ValueError("not YYYY-MM-DD")
    if not isinstance(v, (str, date)):
        raise ValueError("not a date")
    return v


def _not_past(v: date, info: ValidationInfo) -> date:
    if v < info.context["today"]():
        raise ValueError("date in the past")
    return v


def _email(v: str) -> str:
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", v):
        raise ValueError("not an email")
    return v


def _digits_as_str(v: Any) -> Any:
    return str(v) if isinstance(v, int) and not isinstance(v, bool) else v


@dataclass(frozen=True)
class Format:
    type: Any
    message: str  # formatted with label
    fix: str  # formatted with ref (Object.Field) and crm (Salesforce/HubSpot)


FORMATS: dict[str, Format] = {
    "text": Format(
        Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)],
        "{label} must be non-blank text of at most 255 characters.",
        "Set {ref} in {crm} to a non-blank value of at most 255 characters.",
    ),
    "url": Format(
        Annotated[str, StringConstraints(strip_whitespace=True), AfterValidator(_url)],
        "{label} must be an http or https URL with a host.",
        "Set {ref} in {crm} to a URL such as https://northwind.example.",
    ),
    "country": Format(
        Literal[COUNTRIES],
        "{label} must be one of United States, India, Germany, or United Kingdom.",
        "Set {ref} in {crm} to United States, India, Germany, or United Kingdom.",
    ),
    "positive_number": Format(
        Annotated[int | float, Field(gt=0)],
        "{label} must be a number greater than 0.",
        "Set {ref} in {crm} to a number greater than 0, such as 5000.",
    ),
    "future_date": Format(
        Annotated[date, BeforeValidator(_iso_date_string), AfterValidator(_not_past)],
        "{label} must be a YYYY-MM-DD date of today or later (UTC).",
        "Set {ref} in {crm} to today or a later date, formatted YYYY-MM-DD.",
    ),
    "email": Format(
        Annotated[str, StringConstraints(strip_whitespace=True), AfterValidator(_email)],
        "{label} must be an email address.",
        "Set {ref} in {crm} to an email address such as ada@northwind.example.",
    ),
    "duns": Format(
        Annotated[str, BeforeValidator(_digits_as_str), StringConstraints(pattern=r"^\d{9}$")],
        "{label} must be exactly 9 digits.",
        "Set {ref} in {crm} to a 9-digit DUNS number such as 123456789, or clear it.",
    ),
}

# Format used when a field is introduced without one. Keeps the documented request body sufficient.
DEFAULT_FORMATS = {"customer_duns": "duns"}


def is_blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def error(field, crm_object, crm_field, received, message, fix) -> dict:
    return {
        "field": field,
        "crm_object": crm_object,
        "crm_field": crm_field,
        "received": received,
        "message": message,
        "fix": fix,
    }


def build_referral(
    template_id: str,
    crm_type: str,
    fields: list[dict],
    values: dict[tuple[str, str], Any],
    today: Callable[[], date],
) -> tuple[dict | None, list[dict]]:
    """fields: the template view rows. values: (crm_object, crm_field) -> raw CRM value, for fetched fields only."""
    # A field made required after a customer hid it is validated anyway: AWS requirements beat the old choice.
    visible = [f for f in fields if not f["unused"] or f["required"]]
    model = create_model(
        "Referral",
        **{
            f["labra_field"]: (FORMATS[f["format"]].type, ...) if f["required"]
            else (FORMATS[f["format"]].type | None, None)
            for f in visible
        },
    )

    data = {}
    for f in visible:
        if f["mapped"]:
            value = values.get((f["crm_object"], f["crm_field"]))
            if not is_blank(value):
                data[f["labra_field"]] = value

    try:
        referral = model.model_validate(data, context={"today": today})
    except ValidationError as exc:
        by_name = {f["labra_field"]: f for f in fields}
        crm = CRM_NAMES[crm_type]
        errors, seen = [], set()
        for e in exc.errors():
            name = e["loc"][0]
            if name in seen:
                continue  # int|float union reports one error per branch
            seen.add(name)
            f = by_name[name]
            received = values.get((f["crm_object"], f["crm_field"])) if f["mapped"] else None
            ref = f"{f['crm_object']}.{f['crm_field']}"
            if e["type"] != "missing":
                fmt = FORMATS[f["format"]]
                errors.append(error(name, f["crm_object"], f["crm_field"], received,
                                    fmt.message.format(label=f["label"]), fmt.fix.format(ref=ref, crm=crm)))
            elif f["mapped"]:
                errors.append(error(name, f["crm_object"], f["crm_field"], received,
                                    f"{f['label']} is required but {ref} is empty on this record.",
                                    f"Fill in {ref} in {crm}."))
            else:
                errors.append(error(name, None, None, None,
                                    f"{f['label']} is required but template {template_id} does not map it.",
                                    f"Map {name} to a {crm} object and field on template {template_id}."))
        return None, errors

    return referral.model_dump(mode="json", exclude_none=True), []
