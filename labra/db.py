import sqlite3
import sys
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS labra_fields (
    name        TEXT PRIMARY KEY,
    label       TEXT NOT NULL,
    required    INTEGER NOT NULL DEFAULT 0,
    format      TEXT NOT NULL DEFAULT 'text',
    position    INTEGER NOT NULL,
    updated_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS mapping_templates (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    crm_type    TEXT NOT NULL CHECK (crm_type IN ('salesforce', 'hubspot')),
    created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Only customer choices live here: one row per Labra field the customer mapped or hid.
-- A Labra field with no row is shown as unmapped; adding a Labra field writes no rows here.
CREATE TABLE IF NOT EXISTS template_field_mappings (
    template_id  TEXT NOT NULL REFERENCES mapping_templates(id) ON DELETE CASCADE,
    labra_field  TEXT NOT NULL REFERENCES labra_fields(name),
    crm_object   TEXT,
    crm_field    TEXT,
    unused       INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (template_id, labra_field),
    UNIQUE (template_id, crm_object, crm_field),
    CHECK (unused = 1 OR (crm_object IS NOT NULL AND crm_field IS NOT NULL))
);
"""

SEED_FIELDS = [
    ("company_name", "Company name", "text"),
    ("website", "Website", "url"),
    ("country", "Country", "country"),
    ("expected_monthly_revenue", "Expected monthly revenue", "positive_number"),
    ("target_close_date", "Target close date", "future_date"),
    ("primary_sales_rep_email", "Primary sales rep email", "email"),
]


def init_db(path: str) -> None:
    with connect(path) as conn:
        conn.executescript(SCHEMA)
        conn.executemany(
            "INSERT OR IGNORE INTO labra_fields (name, label, required, format, position) VALUES (?, ?, 1, ?, ?)",
            [(name, label, fmt, i) for i, (name, label, fmt) in enumerate(SEED_FIELDS)],
        )


@contextmanager
def connect(path: str):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        with conn:
            yield conn
    finally:
        conn.close()


if __name__ == "__main__":
    db_path = sys.argv[1] if len(sys.argv) > 1 else "labra.db"
    init_db(db_path)
    print(f"initialized {db_path}")
