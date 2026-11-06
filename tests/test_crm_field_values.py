from conftest import CONTOSO, DUNS, seed_northwind

from labra.crm import FakeCrm, RateLimited

URL = "/mapping-templates/northwind-default/crm-field-values"

SUCCESS = {
    "referral": {
        "company_name": "Northwind Traders",
        "website": "https://northwind.example",
        "country": "United States",
        "expected_monthly_revenue": 5000,
        "target_close_date": "2026-12-01",
        "primary_sales_rep_email": "ada@northwind.example",
    },
    "errors": [],
}

NORTHWIND_CALLS = [
    {"object": "Account", "fields": ["Name", "Website", "BillingCountry"]},
    {"object": "Opportunity", "fields": ["Amount", "CloseDate"]},
    {"object": "Contact", "fields": ["Email"]},
]


def test_valid_referral_one_call_per_object(client, crm, northwind):
    seed_northwind(crm)
    r = client.post(URL, json={"record_id": "opp-100"})
    assert r.status_code == 200
    assert r.json() == SUCCESS
    assert crm.calls == NORTHWIND_CALLS


def test_extra_crm_key_is_not_requested_or_copied(client, crm, northwind):
    seed_northwind(crm)
    referral = client.post(URL, json={"record_id": "opp-100"}).json()["referral"]
    assert "InternalNotes" not in referral and "do not copy me" not in referral.values()
    assert all("InternalNotes" not in c["fields"] for c in crm.calls)


def test_numeric_string_amount_is_coerced(client, crm, northwind):
    seed_northwind(crm, opportunity={"Amount": "5000", "CloseDate": "2026-12-01"})
    referral = client.post(URL, json={"record_id": "opp-100"}).json()["referral"]
    assert referral["expected_monthly_revenue"] == 5000
    assert isinstance(referral["expected_monthly_revenue"], int)


def test_invalid_seed_reports_every_field(client, crm, northwind):
    seed_northwind(
        crm,
        account={"Name": " ", "Website": "not a url", "BillingCountry": "USA"},
        opportunity={"Amount": -10, "CloseDate": "2020-01-01"},
        contact={"Email": "ada"},
    )
    r = client.post(URL, json={"record_id": "opp-100"})
    assert r.status_code == 422
    body = r.json()
    assert body["referral"] is None
    got = {(e["crm_object"], e["crm_field"]): e for e in body["errors"]}
    expected = {
        ("Account", "Name"): (" ", "company_name", "Fill in Account.Name in Salesforce."),
        ("Account", "Website"): ("not a url", "website",
                                 "Set Account.Website in Salesforce to a URL such as https://northwind.example."),
        ("Account", "BillingCountry"): ("USA", "country", "Set Account.BillingCountry in Salesforce to "
                                                          "United States, India, Germany, or United Kingdom."),
        ("Opportunity", "Amount"): (-10, "expected_monthly_revenue",
                                    "Set Opportunity.Amount in Salesforce to a number greater than 0, such as 5000."),
        ("Opportunity", "CloseDate"): ("2020-01-01", "target_close_date", "Set Opportunity.CloseDate in Salesforce "
                                                                          "to today or a later date, formatted YYYY-MM-DD."),
        ("Contact", "Email"): ("ada", "primary_sales_rep_email",
                               "Set Contact.Email in Salesforce to an email address such as ada@northwind.example."),
    }
    assert set(got) == set(expected)
    for key, (received, labra_field, fix) in expected.items():
        e = got[key]
        assert (e["received"], e["field"], e["fix"]) == (received, labra_field, fix)
        assert e["message"] and "value_error" not in e["message"].lower() and "Value error" not in e["message"]


def test_single_rate_limit_is_retried(client, crm, northwind):
    seed_northwind(crm)
    crm.fail_next("Opportunity")
    r = client.post(URL, json={"record_id": "opp-100"})
    assert r.status_code == 200
    assert r.json() == SUCCESS
    assert [c["object"] for c in crm.calls] == ["Account", "Opportunity", "Opportunity", "Contact"]


class AlwaysLimited(FakeCrm):
    def __init__(self, limited):
        super().__init__()
        self.limited = limited

    def fetch(self, object_name, record_id, fields):
        if object_name == self.limited:
            self.calls.append({"object": object_name, "fields": list(fields)})
            raise RateLimited(object_name, retry_after_ms=5)
        return super().fetch(object_name, record_id, fields)


def test_second_rate_limit_ends_request_without_missing_field_errors(tmp_path):
    from fastapi.testclient import TestClient

    from conftest import NORTHWIND, TODAY
    from labra.app import create_app

    crm = AlwaysLimited("Opportunity")
    seed_northwind(crm)
    client = TestClient(create_app(crm, db_path=str(tmp_path / "t.db"), today=lambda: TODAY))
    client.post("/mapping-templates", json=NORTHWIND)

    r = client.post(URL, json={"record_id": "opp-100"})
    assert r.status_code == 503
    body = r.json()
    assert body["referral"] is None
    [err] = body["errors"]
    assert err["crm_object"] == "Opportunity"
    assert "rate limit held" in err["message"]
    assert not any(e["field"] in ("expected_monthly_revenue", "target_close_date") for e in body["errors"])
    assert [c["object"] for c in crm.calls].count("Opportunity") == 2


def test_unused_contact_fields_are_not_fetched(client, crm, northwind):
    seed_northwind(crm)
    client.post("/labra-fields", json={"labra_field": "primary_sales_rep_email",
                                        "label": "Primary sales rep email", "required": False})
    r = client.put("/mapping-templates/northwind-default/fields/primary_sales_rep_email", json={"unused": True})
    assert r.status_code == 200, r.json()

    r = client.post(URL, json={"record_id": "opp-100"})
    assert r.status_code == 200, r.json()
    assert "primary_sales_rep_email" not in r.json()["referral"]
    assert "Contact" not in [c["object"] for c in crm.calls]


def test_unknown_template_and_unknown_record(client, crm, northwind):
    seed_northwind(crm)
    assert client.post("/mapping-templates/nope/crm-field-values", json={"record_id": "opp-100"}).status_code == 404
    r = client.post(URL, json={"record_id": "opp-999"})
    assert r.status_code == 404
    assert r.json()["referral"] is None


def test_new_optional_field_does_not_break_old_referrals(client, crm, northwind):
    seed_northwind(crm)
    client.post("/labra-fields", json=DUNS)
    r = client.post(URL, json={"record_id": "opp-100"})
    assert r.json() == SUCCESS
    assert crm.calls == NORTHWIND_CALLS


def test_mapped_duns_is_validated(client, crm, northwind):
    client.post("/labra-fields", json=DUNS)
    client.put("/mapping-templates/northwind-default/fields/customer_duns",
               json={"crm_object": "Account", "crm_field": "DUNS__c"})
    account = {"Name": "Northwind Traders", "Website": "https://northwind.example", "BillingCountry": "India"}

    seed_northwind(crm, account={**account, "DUNS__c": "123456789"})
    assert client.post(URL, json={"record_id": "opp-100"}).json()["referral"]["customer_duns"] == "123456789"
    assert crm.calls[0] == {"object": "Account", "fields": ["Name", "Website", "BillingCountry", "DUNS__c"]}

    seed_northwind(crm, account={**account, "DUNS__c": "ABC"})
    [err] = client.post(URL, json={"record_id": "opp-100"}).json()["errors"]
    assert (err["field"], err["crm_field"], err["received"]) == ("customer_duns", "DUNS__c", "ABC")

    seed_northwind(crm, account={**account, "DUNS__c": ""})
    assert "customer_duns" not in client.post(URL, json={"record_id": "opp-100"}).json()["referral"]


def test_required_new_field_missing_fails_with_fix(client, crm, northwind):
    seed_northwind(crm)
    client.post("/labra-fields", json={**DUNS, "required": True})
    [err] = client.post(URL, json={"record_id": "opp-100"}).json()["errors"]
    assert err["field"] == "customer_duns" and "customer_duns" in err["fix"]


def test_contoso_hubspot(client, crm):
    assert client.post("/mapping-templates", json=CONTOSO).status_code == 201
    crm.seed("companies", "deal-200", {"name": "Contoso", "website": "https://contoso.example", "country": "Germany"})
    crm.seed("deals", "deal-200", {"amount": "1200.50", "closedate": "2026-12-15"})
    crm.seed("contacts", "deal-200", {"email": "lee@contoso.example"})

    r = client.post("/mapping-templates/contoso-default/crm-field-values", json={"record_id": "deal-200"})
    assert r.status_code == 200, r.json()
    assert r.json()["referral"]["expected_monthly_revenue"] == 1200.5
    assert crm.calls == [
        {"object": "companies", "fields": ["name", "website", "country"]},
        {"object": "deals", "fields": ["amount", "closedate"]},
        {"object": "contacts", "fields": ["email"]},
    ]
