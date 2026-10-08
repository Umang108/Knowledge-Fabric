"""Demo data for local testing: the knowledge bases from the mockups, built from data/samples with
reviewed schemas (what an owner would approve on the Review screen), so the UI can be explored
without waiting for LLM extraction. Used by `python -m app.cli seed-demo-data` and the tests."""

import json
from pathlib import Path

from app import extraction, jobs, kb, pipelines
from app import graph_schema as gs
from app.auth import CurrentUser
from app.db import get_conn
from app.extraction import sheets_summary
from app.graphstore import GraphStore
from app.tabular import read_table_file

SAMPLES = Path("/data/samples")


def _p(name, column, type_="string"):
    return {"name": name, "column": column, "type": type_}


def retail_schema(samples: Path = SAMPLES) -> dict:
    path = samples / "supplier_orders.xlsx"
    return {
        "source_file": "supplier_orders.xlsx",
        "source_path": str(path),
        "sheets": sheets_summary(read_table_file(path)),
        "nodes": [
            {
                "id": "n1",
                "label": "Supplier",
                "sheet": "Suppliers",
                "role": "row",
                "key": {"name": "supplier_id", "column": "Supplier ID"},
                "properties": [
                    _p("name", "Supplier Name"),
                    _p("country", "Country"),
                    _p("rating", "Rating (1-5)", "float"),
                    _p("contact_person", "Contact Person"),
                    _p("contact_email", "Contact Email"),
                    _p("contact_phone", "Contact Phone"),
                    _p("bank_account", "Bank Account"),
                    _p("onboarded", "Onboarded", "date"),
                ],
            },
            {
                "id": "n2",
                "label": "Product",
                "sheet": "Products",
                "role": "row",
                "key": {"name": "sku", "column": "SKU"},
                "properties": [
                    _p("name", "Product"),
                    _p("category", "Category"),
                    _p("unit_price", "Unit Price", "float"),
                    _p("discontinued", "Discontinued", "boolean"),
                ],
            },
            {
                "id": "n3",
                "label": "Warehouse",
                "sheet": "Warehouses",
                "role": "row",
                "key": {"name": "warehouse_id", "column": "WH Code"},
                "properties": [
                    _p("city", "City"),
                    _p("capacity", "Capacity (units)", "integer"),
                    _p("manager", "Manager"),
                    _p("manager_phone", "Manager Phone"),
                ],
            },
            {
                "id": "n4",
                "label": "Customer",
                "sheet": "Customers",
                "role": "row",
                "key": {"name": "customer_id", "column": "customer_id"},
                "properties": [
                    _p("name", "Name"),
                    _p("email", "Email"),
                    _p("phone", "Phone"),
                    _p("dob", "DOB", "date"),
                    _p("pan", "PAN"),
                    _p("address", "Address"),
                    _p("region", "Region"),
                ],
            },
            {
                "id": "n5",
                "label": "Order",
                "sheet": "Orders",
                "role": "row",
                "key": {"name": "order_id", "column": "order_id"},
                "properties": [
                    _p("order_date", "order_date", "date"),
                    _p("shipped_on", "shipped_on", "date"),
                    _p("status", "status"),
                ],
            },
        ],
        "relationships": [
            {
                "id": "r1",
                "type": "SUPPLIES",
                "sheet": "Products",
                "from": {"label": "Supplier", "column": "Supplier"},
                "to": {"label": "Product", "column": "SKU"},
                "properties": [],
                "required": True,
            },
            {
                "id": "r2",
                "type": "SUBSTITUTE_OF",
                "sheet": "Products",
                "from": {"label": "Product", "column": "SKU"},
                "to": {"label": "Product", "column": "Substitute SKU"},
                "properties": [],
                "required": False,
            },
            {
                "id": "r3",
                "type": "STORED_IN",
                "sheet": "Inventory",
                "from": {"label": "Product", "column": "sku"},
                "to": {"label": "Warehouse", "column": "wh_code"},
                "properties": [_p("stock_qty", "stock_qty", "integer"), _p("last_counted", "last_counted", "date")],
                "required": True,
            },
            {
                "id": "r4",
                "type": "PLACED",
                "sheet": "Orders",
                "from": {"label": "Customer", "column": "customer_id"},
                "to": {"label": "Order", "column": "order_id"},
                "properties": [],
                "required": True,
            },
            {
                "id": "r5",
                "type": "CONTAINS",
                "sheet": "Orders",
                "from": {"label": "Order", "column": "order_id"},
                "to": {"label": "Product", "column": "sku"},
                "properties": [_p("qty", "qty", "integer"), _p("notes", "notes")],
                "required": True,
            },
            {
                "id": "r6",
                "type": "SHIPPED_FROM",
                "sheet": "Orders",
                "from": {"label": "Order", "column": "order_id"},
                "to": {"label": "Warehouse", "column": "warehouse"},
                "properties": [],
                "required": True,
            },
        ],
        "pii": [],
    }


def finance_schema(samples: Path = SAMPLES) -> dict:
    path = samples / "finance_ledger.csv"
    return {
        "source_file": "finance_ledger.csv",
        "source_path": str(path),
        "sheets": sheets_summary(read_table_file(path)),
        "nodes": [
            {
                "id": "n1",
                "label": "LedgerEntry",
                "sheet": "finance_ledger",
                "role": "row",
                "key": {"name": "entry_no", "column": "Entry No"},
                "properties": [
                    _p("posting_date", "Posting Date", "date"),
                    _p("amount", "Amount", "float"),
                    _p("currency", "Currency"),
                ],
            },
            {
                "id": "n2",
                "label": "Account",
                "sheet": "finance_ledger",
                "role": "embedded",
                "key": {"name": "gl_account", "column": "GL Account"},
                "properties": [_p("account_name", "Account Name")],
            },
            {
                "id": "n3",
                "label": "CostCentre",
                "sheet": "finance_ledger",
                "role": "embedded",
                "key": {"name": "code", "column": "Cost Centre"},
                "properties": [],
            },
            {
                "id": "n4",
                "label": "Vendor",
                "sheet": "finance_ledger",
                "role": "embedded",
                "key": {"name": "name", "column": "Vendor"},
                "properties": [],
            },
            {
                "id": "n5",
                "label": "Approver",
                "sheet": "finance_ledger",
                "role": "embedded",
                "key": {"name": "name", "column": "Approved By"},
                "properties": [_p("email", "Approver Email")],
            },
        ],
        "relationships": [
            {
                "id": "r1",
                "type": "POSTED_TO",
                "sheet": "finance_ledger",
                "from": {"label": "LedgerEntry", "column": "Entry No"},
                "to": {"label": "Account", "column": "GL Account"},
                "properties": [],
                "required": True,
            },
            {
                "id": "r2",
                "type": "CHARGED_TO",
                "sheet": "finance_ledger",
                "from": {"label": "LedgerEntry", "column": "Entry No"},
                "to": {"label": "CostCentre", "column": "Cost Centre"},
                "properties": [],
                "required": True,
            },
            {
                "id": "r3",
                "type": "PAID_TO",
                "sheet": "finance_ledger",
                "from": {"label": "LedgerEntry", "column": "Entry No"},
                "to": {"label": "Vendor", "column": "Vendor"},
                "properties": [],
                "required": True,
            },
            {
                "id": "r4",
                "type": "APPROVED_BY",
                "sheet": "finance_ledger",
                "from": {"label": "LedgerEntry", "column": "Entry No"},
                "to": {"label": "Approver", "column": "Approved By"},
                "properties": [],
                "required": True,
            },
        ],
        "pii": [],
    }


def rules_pii(schema: dict, sheets) -> list[dict]:
    """PII from the pattern/column-name rules only (no LLM), for quick seeding."""
    out = []
    for s in sheets:
        for col in extraction.stored_columns(schema, s.name):
            rule = extraction.rule_pii(s, col)
            if rule:
                out.append({"sheet": s.name, "column": col, **rule, "detected_by": "rules"})
    return out


def _user(uid: str) -> CurrentUser:
    with get_conn() as conn:
        r = conn.execute("SELECT user_id, display_name, email FROM users WHERE user_id = %s", (uid,)).fetchone()
    return CurrentUser(r["user_id"], r["display_name"], r["email"])


def _graph(name, owner, domain, sub, schema, use_llm_pii, approve=True):
    sheets = read_table_file(schema["source_path"], schema["source_file"])
    schema = gs.clean(schema)
    schema["pii"] = (
        [
            {"sheet": s.name, **p}
            for s in sheets
            for p in extraction.detect_pii(s, extraction.stored_columns(schema, s.name))
        ]
        if use_llm_pii
        else rules_pii(schema, sheets)
    )
    by_name = {s.name: s for s in sheets}
    for n in schema["nodes"]:
        n["count"] = len(extraction.key_values(by_name[n["sheet"]], n["key"]["column"]))
    kb.create(_user(owner), name, "graph", domain, sub, GraphStore(name).storage_ref)
    blob = json.dumps(schema, default=str)
    if not approve:
        kb.set_status(name, "awaiting_review", None, draft_schema=blob)
        pipelines.sync_graph_pii(name, schema)
        return
    with get_conn() as conn:
        conn.execute(
            """UPDATE kb_catalog SET status = 'building', draft_schema = %s, approved_schema = %s,
                        approved_cypher = %s, approved_by = %s, approved_at = now(), modified_by = %s
                        WHERE kb_name = %s""",
            (blob, blob, "\n".join(gs.preview(schema)), owner, owner, name),
        )
    pipelines.sync_graph_pii(name, schema)
    job_id = jobs.create(name, "graph_build", pipelines.BUILD_STEPS, owner, schema["source_file"])
    pipelines.graph_build(job_id, name, owner)
    with get_conn() as conn:
        conn.execute(
            "UPDATE jobs SET status = 'succeeded', progress = 100, finished_at = now() WHERE id = %s", (job_id,)
        )


def seed(samples: Path = SAMPLES, use_llm_pii: bool = False, log=print) -> None:
    """Idempotent: existing knowledge bases are left alone."""
    exists = lambda n: kb.get_catalog(n) is not None  # noqa: E731
    if not exists("retail_supply_chain_kg"):
        log("building retail_supply_chain_kg (graph, owner priya.nair)")
        _graph("retail_supply_chain_kg", "priya.nair", "Retail", "Supply chain", retail_schema(samples), use_llm_pii)
        owner = _user("priya.nair")
        for u in ("arjun.mehta", "sneha.iyer", "karthik.r"):
            kb.grant(owner, "retail_supply_chain_kg", u)
    if not exists("finance_ledger_kg"):
        log("building finance_ledger_kg (graph, owner arjun.mehta, priya.nair has user access)")
        _graph("finance_ledger_kg", "arjun.mehta", "Finance", "Ledger", finance_schema(samples), use_llm_pii)
        kb.grant(_user("arjun.mehta"), "finance_ledger_kg", "priya.nair")
    if not exists("retail_policies_rag"):
        log("indexing retail_policies_rag (RAG, owner priya.nair)")
        kb.create(
            _user("priya.nair"), "retail_policies_rag", "rag", "Retail", "Policies", "turboquant:retail_policies_rag"
        )
        files = [(str(samples / n), n) for n in ("returns_policy.pdf", "vendor_handbook.docx", "warehouse_sop.txt")]
        job_id = jobs.create(
            "retail_policies_rag", "rag_ingest", pipelines.RAG_STEPS, "priya.nair", ", ".join(n for _, n in files)
        )
        pipelines.rag_ingest(job_id, "retail_policies_rag", files, "priya.nair", "rag_ingest", pii_llm=use_llm_pii)
        with get_conn() as conn:
            conn.execute(
                "UPDATE jobs SET status = 'succeeded', progress = 100, finished_at = now() WHERE id = %s", (job_id,)
            )
    if not exists("supplier_orders_review_kg"):
        log("creating supplier_orders_review_kg (draft awaiting review, to try the Review screen)")
        _graph(
            "supplier_orders_review_kg",
            "priya.nair",
            "Retail",
            "Supply chain",
            retail_schema(samples),
            use_llm_pii,
            approve=False,
        )
