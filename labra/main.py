"""Local server wired to a seeded FakeCrm: `uv run uvicorn labra.main:app`."""

from labra.app import create_app
from labra.crm import FakeCrm

crm = FakeCrm()
crm.seed("Account", "opp-100", {
    "Name": "Northwind Traders",
    "Website": "https://northwind.example",
    "BillingCountry": "United States",
    "InternalNotes": "do not copy me",
})
crm.seed("Opportunity", "opp-100", {"Amount": 5000, "CloseDate": "2026-12-01"})
crm.seed("Contact", "opp-100", {"Email": "ada@northwind.example"})
crm.seed("companies", "deal-200", {"name": "Contoso", "website": "https://contoso.example", "country": "Germany"})
crm.seed("deals", "deal-200", {"amount": "1200.50", "closedate": "2026-12-15"})
crm.seed("contacts", "deal-200", {"email": "lee@contoso.example"})

app = create_app(crm)
