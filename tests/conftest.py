from datetime import date

import pytest
from fastapi.testclient import TestClient

from labra.app import create_app
from labra.crm import FakeCrm

TODAY = date(2026, 11, 4)

NORTHWIND = {
    "id": "northwind-default",
    "name": "Northwind default",
    "crm_type": "salesforce",
    "mappings": [
        {"labra_field": "company_name", "crm_object": "Account", "crm_field": "Name"},
        {"labra_field": "website", "crm_object": "Account", "crm_field": "Website"},
        {"labra_field": "country", "crm_object": "Account", "crm_field": "BillingCountry"},
        {"labra_field": "expected_monthly_revenue", "crm_object": "Opportunity", "crm_field": "Amount"},
        {"labra_field": "target_close_date", "crm_object": "Opportunity", "crm_field": "CloseDate"},
        {"labra_field": "primary_sales_rep_email", "crm_object": "Contact", "crm_field": "Email"},
    ],
}

CONTOSO = {
    "id": "contoso-default",
    "name": "Contoso default",
    "crm_type": "hubspot",
    "mappings": [
        {"labra_field": "company_name", "crm_object": "companies", "crm_field": "name"},
        {"labra_field": "website", "crm_object": "companies", "crm_field": "website"},
        {"labra_field": "country", "crm_object": "companies", "crm_field": "country"},
        {"labra_field": "expected_monthly_revenue", "crm_object": "deals", "crm_field": "amount"},
        {"labra_field": "target_close_date", "crm_object": "deals", "crm_field": "closedate"},
        {"labra_field": "primary_sales_rep_email", "crm_object": "contacts", "crm_field": "email"},
    ],
}

DUNS = {"labra_field": "customer_duns", "label": "Customer DUNS", "required": False}


def seed_northwind(crm, record_id="opp-100", account=None, opportunity=None, contact=None):
    crm.seed("Account", record_id, account or {
        "Name": "Northwind Traders",
        "Website": "https://northwind.example",
        "BillingCountry": "United States",
        "InternalNotes": "do not copy me",
    })
    crm.seed("Opportunity", record_id, opportunity or {"Amount": 5000, "CloseDate": "2026-12-01"})
    crm.seed("Contact", record_id, contact or {"Email": "ada@northwind.example"})


@pytest.fixture
def crm():
    return FakeCrm()


@pytest.fixture
def client(crm, tmp_path):
    return TestClient(create_app(crm, db_path=str(tmp_path / "test.db"), today=lambda: TODAY))


@pytest.fixture
def northwind(client):
    r = client.post("/mapping-templates", json=NORTHWIND)
    assert r.status_code == 201, r.json()
    return r.json()
