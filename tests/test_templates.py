import copy
import sqlite3

import pytest

from conftest import CONTOSO, DUNS, NORTHWIND


def field(template, name):
    return next(f for f in template["fields"] if f["labra_field"] == name)


def test_create_northwind_and_get(client, northwind):
    body = client.get("/mapping-templates/northwind-default").json()
    assert body["crm_type"] == "salesforce"
    got = {f["labra_field"]: (f["crm_object"], f["crm_field"], f["mapped"]) for f in body["fields"]}
    assert got == {m["labra_field"]: (m["crm_object"], m["crm_field"], True) for m in NORTHWIND["mappings"]}


def test_mappings_belong_to_one_template(client, northwind):
    assert client.post("/mapping-templates", json=CONTOSO).status_code == 201
    assert field(client.get("/mapping-templates/contoso-default").json(), "company_name")["crm_object"] == "companies"
    assert field(client.get("/mapping-templates/northwind-default").json(), "company_name")["crm_object"] == "Account"


def test_new_field_appears_on_existing_template_without_rewriting_it(client, northwind, tmp_path):
    db = sqlite3.connect(tmp_path / "test.db")
    before = db.execute("SELECT * FROM template_field_mappings ORDER BY labra_field").fetchall()

    r = client.post("/labra-fields", json=DUNS)
    assert r.status_code == 200, r.json()

    body = client.get("/mapping-templates/northwind-default").json()
    assert field(body, "customer_duns") == {
        "labra_field": "customer_duns", "label": "Customer DUNS", "required": False, "format": "duns",
        "crm_object": None, "crm_field": None, "mapped": False, "unused": False,
    }
    assert (field(body, "company_name")["crm_object"], field(body, "company_name")["crm_field"]) == ("Account", "Name")
    assert db.execute("SELECT * FROM template_field_mappings ORDER BY labra_field").fetchall() == before


def test_reintroducing_field_updates_definition_and_keeps_mappings(client, northwind):
    client.post("/labra-fields", json=DUNS)
    client.put("/mapping-templates/northwind-default/fields/customer_duns",
               json={"crm_object": "Account", "crm_field": "DUNS__c"})
    client.post("/labra-fields", json={**DUNS, "label": "DUNS number"})

    f = field(client.get("/mapping-templates/northwind-default").json(), "customer_duns")
    assert (f["label"], f["crm_object"], f["crm_field"]) == ("DUNS number", "Account", "DUNS__c")


def test_unused_survives_label_change(client, northwind):
    client.post("/labra-fields", json=DUNS)
    r = client.put("/mapping-templates/northwind-default/fields/customer_duns", json={"unused": True})
    assert r.status_code == 200, r.json()
    client.post("/labra-fields", json={**DUNS, "label": "D-U-N-S number"})

    f = field(client.get("/mapping-templates/northwind-default").json(), "customer_duns")
    assert f["unused"] is True and f["label"] == "D-U-N-S number"


def test_required_field_cannot_be_unused(client, northwind):
    r = client.put("/mapping-templates/northwind-default/fields/company_name", json={"unused": True})
    assert r.status_code == 422
    [err] = r.json()["errors"]
    assert err["field"] == "company_name"
    assert "required by AWS" in err["message"]
    assert field(client.get("/mapping-templates/northwind-default").json(), "company_name")["unused"] is False


def _mutated(fn):
    body = copy.deepcopy(NORTHWIND)
    fn(body)
    return body


@pytest.mark.parametrize("body, fix", [
    (_mutated(lambda b: b.update(name="  ")), "Send a non-empty template name."),
    (_mutated(lambda b: b.update(crm_type="pipedrive")), "Send salesforce or hubspot."),
    (_mutated(lambda b: b["mappings"].append(
        {"labra_field": "favourite_colour", "crm_object": "Account", "crm_field": "Colour"})),
     "Remove that mapping or add the field first."),
    (_mutated(lambda b: b["mappings"].append(
        {"labra_field": "company_name", "crm_object": "Account", "crm_field": "LegalName"})),
     "Keep one CRM object and field per Labra field."),
    (_mutated(lambda b: b["mappings"][1].update(crm_field="Name")), "Pick one Labra field for that CRM value."),
    (_mutated(lambda b: b["mappings"][1].update(crm_field=" ")), "Fill both, or mark the field unused."),
    (_mutated(lambda b: b["mappings"].pop()), "Map that field before saving."),
])
def test_invalid_create_is_rejected_and_stores_nothing(client, body, fix):
    r = client.post("/mapping-templates", json=body)
    assert r.status_code == 422
    assert fix in [e["fix"] for e in r.json()["errors"]]
    assert client.get("/mapping-templates/northwind-default").status_code == 404


def test_unknown_template(client):
    assert client.get("/mapping-templates/nope").status_code == 404
