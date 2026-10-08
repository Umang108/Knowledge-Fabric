"""PII on the Review screen: reviewers mark, unmark and recategorise PII columns themselves.

The LLM/rules findings arrive as status "detected"; a reviewer's choice is stored as "confirmed" or
"dismissed" (dismissed findings are kept so the decision is audited) and synced to kb_pii_fields."""

import json

import pytest

from app import extraction, kb
from app import graph_schema as gs
from app.auth import CurrentUser
from app.cli import main as cli
from app.db import get_conn
from app.tabular import read_table_file

from .conftest import login
from .fixtures import SAMPLES

KB = "t_pii_review_kg"


@pytest.fixture
def draft(monkeypatch):
    """A graph KB waiting for review, extracted without an LLM (rules-only PII)."""
    cli(["seed-demo-users"])
    monkeypatch.setattr(extraction, "ask_json", lambda *a, **k: (_ for _ in ()).throw(ValueError("no LLM")))
    sheets = read_table_file(SAMPLES / "supplier_orders.xlsx")
    schema = extraction.extract(sheets, "supplier_orders.xlsx")
    schema["source_path"] = str(SAMPLES / "supplier_orders.xlsx")
    owner = CurrentUser("priya.nair", "Priya Nair", None)
    kb.create(owner, KB, "graph", "Retail", "Supply chain", f"label:KB_{KB}")
    kb.set_status(KB, "awaiting_review", None, draft_schema=json.dumps(schema, default=str))
    return schema


def _pii(schema, sheet, column):
    return next((p for p in schema["pii"] if (p["sheet"], p["column"]) == (sheet, column)), None)


def _rows():
    with get_conn() as conn:
        return {
            (r["node_label"], r["property_name"]): r
            for r in conn.execute(
                "SELECT * FROM kb_pii_fields WHERE kb_name = %s AND source_document IS NULL", (KB,)
            ).fetchall()
        }


def test_detected_pii_carries_status(draft):
    assert draft["pii"], "the rules should find e-mail/phone columns in the sample workbook"
    assert all(p["status"] == "detected" and p["detected_by"] in ("rules", "llm") for p in draft["pii"])


def test_clean_pii_validates_and_normalises(draft):
    sheets = draft["sheets"]
    cleaned = extraction.clean_pii(
        [
            {"sheet": "Suppliers", "column": "Contact Person", "category": "PERSON_NAME"},  # added by a person
            {
                "sheet": "Suppliers",
                "column": "Contact Email",
                "category": "email",
                "status": "dismissed",
                "detected_by": "rules",
                "confidence": 7,
            },
            {"sheet": "Suppliers", "column": "Contact Person", "category": "person_name", "status": "confirmed"},
        ],
        sheets,
    )
    by_col = {p["column"]: p for p in cleaned}
    assert len(cleaned) == 2  # one entry per column, the last one wins
    person = by_col["Contact Person"]
    assert {k: person[k] for k in ("sheet", "column", "category", "confidence", "reason", "detected_by", "status")} == {
        "sheet": "Suppliers",
        "column": "Contact Person",
        "category": "person_name",
        "confidence": 1.0,
        "reason": "Marked as PII on the Review screen",
        "detected_by": "user",
        "status": "confirmed",
    }
    # NIST SP 800-122: a name is a direct identifier with low field sensitivity; nothing sensitive is active
    # in the same records (the e-mail was dismissed), so the impact stays low
    assert person["nist_identifier"] == "direct" and person["nist_impact"] == "low" and person["sensitivity"] == "low"
    assert any(f.startswith("identifiability") for f in person["nist_factors"]) and person["nist_controls"]
    assert by_col["Contact Email"]["status"] == "dismissed" and by_col["Contact Email"]["confidence"] == 1.0

    with pytest.raises(gs.SchemaError) as exc:
        extraction.clean_pii(
            [
                {"sheet": "Nope", "column": "x", "category": "email"},
                {"sheet": "Suppliers", "column": "Missing", "category": "email"},
                {"sheet": "Suppliers", "column": "Contact Email", "category": "shoe_size"},
                {"sheet": "Suppliers", "column": "Contact Email", "category": "email", "status": "maybe"},
                "junk",
            ],
            sheets,
        )
    assert len(exc.value.errors) == 5


def test_reviewer_marks_unmarks_and_recategorises(client, draft):
    p = login(client, "priya.nair")
    review = client.get(f"/api/kbs/{KB}/review", headers=p).json()
    assert "person_name" in review["pii_categories"]
    schema = review["schema"]
    email = _pii(schema, "Suppliers", "Contact Email")
    phone = _pii(schema, "Suppliers", "Contact Phone")
    assert email and phone and _pii(schema, "Suppliers", "Supplier Name") is None

    edited = dict(schema)
    edited["pii"] = [
        {**email, "status": "dismissed"},  # "not PII after all"
        {**phone, "category": "other", "status": "confirmed"},  # recategorised
        {
            "sheet": "Suppliers",
            "column": "Supplier Name",
            "category": "person_name",
            "status": "confirmed",
            "detected_by": "user",
        },  # marked by the reviewer
    ] + [x for x in schema["pii"] if x["column"] not in ("Contact Email", "Contact Phone")]

    preview = client.post(f"/api/kbs/{KB}/review/preview", headers=p, json={"schema": edited})
    assert preview.status_code == 200, preview.text
    name = _pii(preview.json()["schema"], "Suppliers", "Supplier Name")
    # a name in the same records as a bank account: NIST context factor raises low -> moderate
    assert name["nist_impact"] == "moderate" and name["sensitivity"] == "medium"
    assert any("Bank Account" in f for f in name["nist_factors"])

    saved = client.put(f"/api/kbs/{KB}/review", headers=p, json={"schema": edited})
    assert saved.status_code == 200, saved.text

    rows = _rows()
    supplier = next(n for n in schema["nodes"] if n["sheet"] == "Suppliers" and n["role"] == "row")
    prop = {pp["column"]: pp["name"] for pp in supplier["properties"]}
    assert rows[(supplier["label"], prop["Contact Email"])]["status"] == "dismissed"
    assert rows[(supplier["label"], prop["Contact Phone"])]["pii_category"] == "other"
    assert rows[(supplier["label"], prop["Contact Phone"])]["status"] == "confirmed"
    added = rows[(supplier["label"], prop["Supplier Name"])]
    assert (added["detected_by"], added["status"], added["modified_by"]) == ("user", "confirmed", "priya.nair")

    # the API's PII list, the draft schema and the active list agree
    stored = kb.get_catalog(KB)["draft_schema"]
    assert {p["column"] for p in extraction.active_pii(stored)} >= {"Contact Phone", "Supplier Name"}
    assert "Contact Email" not in {p["column"] for p in extraction.active_pii(stored)}
    api_rows = client.get(f"/api/kbs/{KB}/pii", headers=p).json()
    assert {r["status"] for r in api_rows} >= {"dismissed", "confirmed"}

    # undo: a person-only mark that is removed again disappears from the table
    edited["pii"] = [x for x in edited["pii"] if x["column"] != "Supplier Name"]
    assert client.put(f"/api/kbs/{KB}/review", headers=p, json={"schema": edited}).status_code == 200
    assert (supplier["label"], prop["Supplier Name"]) not in _rows()


def test_invalid_pii_is_refused_with_the_reason(client, draft):
    p = login(client, "priya.nair")
    schema = client.get(f"/api/kbs/{KB}/review", headers=p).json()["schema"]
    schema["pii"] = [{"sheet": "Suppliers", "column": "Contact Email", "category": "favourite_colour"}]
    r = client.post(f"/api/kbs/{KB}/review/preview", headers=p, json={"schema": schema})
    assert r.status_code == 422
    assert "category must be one of" in r.json()["detail"]["errors"][0]


def test_only_the_owner_edits_pii(client, draft):
    priya = login(client, "priya.nair")
    assert client.post(f"/api/kbs/{KB}/access", headers=priya, json={"user_id": "arjun.mehta"}).status_code == 201
    arjun = login(client, "arjun.mehta")
    schema = client.get(f"/api/kbs/{KB}/review", headers=priya).json()["schema"]
    assert client.put(f"/api/kbs/{KB}/review", headers=arjun, json={"schema": schema}).status_code == 403
