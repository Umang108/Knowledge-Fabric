"""Pure unit tests: value parsing, Cypher safety/scoping, multi-database mode, schema editing."""

import datetime as dt
from unittest.mock import MagicMock

import pytest

from app import graph_schema as gs
from app.graphstore import GraphStore, UnsafeQueryError, check_read_only, database_name, scope_cypher
from app.tabular import (
    TabularError,
    coerce,
    infer_type,
    norm_header,
    parse_bool,
    parse_date,
    parse_number,
    read_table_file,
)

from .fixtures import SAMPLES, manifest, retail_schema


# ------------------------------------------------------------------ parsing
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("₹1,250.00", 1250.0),
        ("INR 1250", 1250),
        ("1,250", 1250),
        ("(1,200.00)", -1200.0),
        ("12,000", 12000),
        (" 42 ", 42),
        ("4.5", 4.5),
        (7, 7),
        ("N/A", None),
        ("+91 98400 11223", None),
        ("Rs. 1,25,000/-", 125000),
        ("\u20b912,50,000", 1250000),
        ("12,34,567.50", 1234567.5),
        ("", None),
        (True, None),
    ],
)
def test_parse_number(raw, expected):
    assert parse_number(raw) == expected


@pytest.mark.parametrize(
    "raw", ["2026-09-22", "22/09/2026", "Sep 22, 2026", dt.datetime(2026, 9, 22), "2026-09-22T10:00:00", "22-Sep-2026"]
)
def test_parse_date_formats(raw):
    assert parse_date(raw) == dt.date(2026, 9, 22)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Y", True),
        ("yes", True),
        ("TRUE", True),
        ("1", True),
        ("N", False),
        ("no", False),
        ("0", False),
        ("maybe", None),
    ],
)
def test_parse_bool(raw, expected):
    assert parse_bool(raw) == expected


def test_type_inference_tolerates_a_few_bad_cells():
    assert infer_type(["4.1", 3.6, "N/A", 4.4] + [4.0] * 30) == "float"
    assert infer_type(["0012", "0345"]) == "string"  # leading zeros stay text
    assert infer_type(["Y", "no", "TRUE"]) == "boolean" and infer_type([1, 0, 1]) == "integer"
    assert coerce(5100.0, "string") == "5100"


def test_header_matching_ignores_case_and_punctuation():
    assert norm_header("Order ID") == norm_header("order_id") == norm_header("ORDER-ID")


def test_reader_handles_the_messy_workbook():
    sheets = {s.name: s for s in read_table_file(SAMPLES / "supplier_orders.xlsx")}
    m = manifest()["files"]["supplier_orders.xlsx"]["sheets"]
    for name, spec in m.items():
        assert sheets[name].header_row == spec["header_row"]
        assert len(sheets[name].rows) == spec["data_rows"]  # blank rows skipped
    assert sheets["Suppliers"].profile["Rating (1-5)"].type == "float"
    assert sheets["Products"].profile["Discontinued"].type == "boolean"
    assert sheets["Orders"].profile["order_date"].type == "date"


def test_reader_handles_bom_semicolon_crlf_csv():
    (s,) = read_table_file(SAMPLES / "finance_ledger.csv")
    assert s.columns[0] == "Entry No" and len(s.rows) == 480
    assert s.profile["Amount"].type == "float" and s.profile["Posting Date"].type == "date"


@pytest.mark.parametrize("name,message", [("corrupt.xlsx", "not a valid Excel"), ("empty.csv", "no data rows")])
def test_reader_rejects_bad_files_clearly(name, message):
    with pytest.raises(TabularError, match=message):
        read_table_file(SAMPLES / "edge_cases" / name)


# ------------------------------------------------------------------ Cypher safety
SCOPED = [
    (
        "MATCH (s:Supplier)-[:SUPPLIES]->(p:Product) RETURN s.name",
        "MATCH (s:Supplier:`KB_x`)-[:SUPPLIES]->(p:Product:`KB_x`) RETURN s.name",
    ),
    ("MATCH (n) RETURN count(n)", "MATCH (n:`KB_x`) RETURN count(n)"),
    ("MATCH ()-[r]->() RETURN count(r)", "MATCH (:`KB_x`)-[r]->(:`KB_x`) RETURN count(r)"),
    ("MATCH (a:A), (b:B) RETURN a", "MATCH (a:A:`KB_x`), (b:B:`KB_x`) RETURN a"),
    ("MATCH p = (a:Order) RETURN p", "MATCH p = (a:Order:`KB_x`) RETURN p"),
    ("MATCH (n:A|B) RETURN n", "MATCH (n:(A|B)&`KB_x`) RETURN n"),
    ("MATCH (n:S) WHERE n.name = '(x:Hack)' RETURN n", "MATCH (n:S:`KB_x`) WHERE n.name = '(x:Hack)' RETURN n"),
    (
        "MATCH (a:O) WHERE EXISTS { (a)-->(b:Y) } RETURN a",
        "MATCH (a:O:`KB_x`) WHERE EXISTS { (a:`KB_x`)-->(b:Y:`KB_x`) } RETURN a",
    ),
    ("RETURN [(a:C)-[:P]->(o) | o.id] AS ids", "RETURN [(a:C:`KB_x`)-[:P]->(o:`KB_x`) | o.id] AS ids"),
    (
        "MATCH x = shortestPath((a:C)-[*..4]-(b:W)) RETURN x",
        "MATCH x = shortestPath((a:C:`KB_x`)-[*..4]-(b:W:`KB_x`)) RETURN x",
    ),
    (
        "MATCH (c:C) OPTIONAL MATCH (c)-[:P]->(o) RETURN count(o)",
        "MATCH (c:C:`KB_x`) OPTIONAL MATCH (c:`KB_x`)-[:P]->(o:`KB_x`) RETURN count(o)",
    ),
]


@pytest.mark.parametrize("query,expected", SCOPED)
def test_every_node_pattern_is_scoped(query, expected):
    assert scope_cypher(query, "KB_x") == expected


@pytest.mark.parametrize(
    "query",
    [
        "MATCH (n) DETACH DELETE n",
        "CREATE (n:X)",
        "MATCH (n) SET n.a = 1",
        "MERGE (n:X)",
        "MATCH (n) REMOVE n.a",
        "CALL db.labels()",
        "USE other MATCH (n) RETURN n",
        "MATCH (n) RETURN n; MATCH (m) DELETE m",
        "LOAD CSV FROM 'file:///x' AS r RETURN r",
        "MATCH (n:KB_other) RETURN n",
        "DROP DATABASE neo4j",
        "MATCH (n) FOREACH (x IN [1] | SET n.a = x)",
    ],
)
def test_writes_and_escapes_are_refused(query):
    with pytest.raises(UnsafeQueryError):
        check_read_only(query)


def test_unscopable_pattern_is_refused_not_leaked():
    with pytest.raises(UnsafeQueryError):
        scope_cypher("MATCH (n:Supplier WHERE toLower(n.name) = 'x') RETURN n", "KB_x")


# ------------------------------------------------------------------ multi-database (Enterprise) mode
def test_multi_mode_uses_one_database_per_kb():
    driver = MagicMock()
    store = GraphStore("retail_supply_chain_kg", mode="multi", driver=driver)
    assert store.database == "retail-supply-chain-kg" == database_name("retail_supply_chain_kg")
    assert store.kb_label is None and store.label("Supplier") == "`Supplier`"
    store.ensure_storage()
    driver.execute_query.assert_called_with(
        "CREATE DATABASE `retail-supply-chain-kg` IF NOT EXISTS WAIT", database_="system"
    )
    store.write("RETURN 1")
    assert driver.execute_query.call_args.kwargs["database_"] == "retail-supply-chain-kg"


def test_multi_mode_chat_query_is_bound_to_the_kb_database_and_unscoped():
    driver = MagicMock()
    session = driver.session.return_value.__enter__.return_value
    session.execute_read.return_value = [{"n": 1}]
    store = GraphStore("finance_ledger_kg", mode="multi", driver=driver)
    executed, rows = store.run_readonly("MATCH (n) RETURN count(n) AS n")
    assert executed == "MATCH (n) RETURN count(n) AS n" and rows == [{"n": 1}]
    assert driver.session.call_args.kwargs["database"] == "finance-ledger-kg"
    assert driver.session.call_args.kwargs["default_access_mode"] == "READ"
    with pytest.raises(UnsafeQueryError):
        store.run_readonly("USE `retail-supply-chain-kg` MATCH (n) RETURN n")


def test_multi_mode_drop_and_single_mode_labels():
    driver = MagicMock()
    GraphStore("x_kb", mode="multi", driver=driver).drop()
    driver.execute_query.assert_called_with("DROP DATABASE `x-kb` IF EXISTS", database_="system")
    single = GraphStore("x_kb", mode="single", driver=MagicMock())
    assert single.label("Order") == "`Order`:`KB_x_kb`" and single.storage_ref == "label:KB_x_kb"
    with pytest.raises(ValueError):
        GraphStore("x_kb", mode="cluster")


def test_write_cypher_includes_kb_label_only_in_single_mode():
    schema = gs.clean(retail_schema())
    supplier = schema["nodes"][0]
    assert ":`Supplier`:`KB_t` {supplier_id: row.supplier_id}" in gs.node_cypher(supplier, "KB_t")
    assert "`KB_" not in gs.node_cypher(supplier, None)


# ------------------------------------------------------------------ schema editing
def test_schema_normalizes_user_edits():
    s = retail_schema()
    s["nodes"][0]["label"] = "supplier company"
    s["nodes"][0]["properties"][0]["name"] = "Supplier Name"
    s["relationships"][0]["type"] = "supplies to"
    s["relationships"][0]["from"]["label"] = "supplier company"
    out = gs.clean(s)
    assert out["nodes"][0]["label"] == "SupplierCompany"
    assert out["nodes"][0]["properties"][0]["name"] == "supplier_name"
    assert out["relationships"][0]["type"] == "SUPPLIES_TO"
    assert out["relationships"][0]["from"]["label"] == "SupplierCompany"


def test_schema_validation_reports_problems():
    s = retail_schema()
    s["nodes"][1]["properties"].append({"name": "ghost", "column": "No Such Column", "type": "string"})
    s["nodes"].append(dict(s["nodes"][0]))
    s["relationships"][0]["to"]["label"] = "Nothing"
    with pytest.raises(gs.SchemaError) as e:
        gs.clean(s)
    text = " ".join(e.value.errors)
    assert "No Such Column" in text and "defined more than once" in text and "Nothing" in text


def test_preview_statement_per_type():
    s = gs.clean(retail_schema())
    lines = gs.preview(s)
    assert len(lines) == len(s["nodes"]) + len(s["relationships"])
    assert lines[0].startswith("MERGE (n:Supplier {supplier_id: row.supplier_id}) SET n.name = row.name")
    assert gs.summary(s) == {"node_types": 5, "entities": 0, "relationship_types": 6, "relationships": 0}


# ------------------------------------------------------------------ Cypher repair (chat)
from app.cypher_repair import repair  # noqa: E402

from .fixtures import finance_schema  # noqa: E402

REPAIRS = [  # typical LLM mistakes -> what actually runs
    (
        "MATCH (s:Supplier)-[:SUPPLIES]->(p:Product)<-[:STORED_IN]-(w:Warehouse {city: 'Chennai'}) RETURN s.name",
        "MATCH (s:Supplier)-[:SUPPLIES]->(p:Product)-[:STORED_IN]->(w:Warehouse {city: 'Chennai'}) RETURN s.name",
        "retail",
    ),
    (
        "MATCH (p:Product {sku: 'SKU-10001'})<-[:STORED_IN]-(w:Warehouse) RETURN sum(w.stock_qty) AS s",
        "MATCH (p:Product {sku: 'SKU-10001'})-[_r1:STORED_IN]->(w:Warehouse) RETURN sum(_r1.stock_qty) AS s",
        "retail",
    ),
    (
        "MATCH (e:LedgerEntry)-[r:CHARGED_TO {code: 'CC-IT'}]->(c:CostCentre) RETURN sum(e.amount)",
        "MATCH (e:LedgerEntry)-[r:CHARGED_TO]->(c:CostCentre {code: 'CC-IT'}) RETURN sum(e.amount)",
        "finance",
    ),
    (
        "MATCH (v:Vendor)-[r:PaidTo]->(e:LedgerEntry) RETURN v.name",
        "MATCH (v:Vendor)<-[r:PAID_TO]-(e:LedgerEntry) RETURN v.name",
        "finance",
    ),
    (
        "MATCH (s:supplier) WHERE s.name = '(x:y)<-[:Z]-(q)' RETURN count(s)",
        "MATCH (s:Supplier) WHERE s.name = '(x:y)<-[:Z]-(q)' RETURN count(s)",
        "retail",
    ),
    (
        "MATCH (w:Warehouse) RETURN w.city ORDER BY w.capacity DESC LIMIT 1",
        "MATCH (w:Warehouse) RETURN w.city ORDER BY w.capacity DESC LIMIT 1",
        "retail",
    ),
    (
        "MATCH (o:Order)-[c:CONTAINS]->(p:Product) RETURN sum(c.qty)",
        "MATCH (o:Order)-[c:CONTAINS]->(p:Product) RETURN sum(c.qty)",
        "retail",
    ),  # correct queries unchanged
]


@pytest.mark.parametrize("query,expected,kb", REPAIRS)
def test_cypher_repair(query, expected, kb):
    schema = gs.clean(retail_schema() if kb == "retail" else finance_schema())
    assert repair(query, schema) == expected


def test_chat_examples_come_from_the_schema():
    from app.chat import examples

    text = examples(gs.clean(finance_schema()), {("CostCentre", "code"): ["CC-IT"]})
    assert "LedgerEntry" in text and "Supplier" not in text


def test_cypher_lint_explains_schema_mistakes():
    from app.cypher_repair import error_hint, lint

    f = gs.clean(finance_schema())
    problems = " ".join(lint("MATCH (v:Vendor)-[r:CHARGED_TO]->(a:Account) RETURN v.name, sum(r.amount)", f))
    assert "CHARGED_TO does not connect Vendor and Account" in problems
    assert "Relationship CHARGED_TO has no property amount (amount belongs to LedgerEntry)" in problems
    assert lint("MATCH (e:LedgerEntry)-[:PAID_TO]->(v:Vendor) RETURN v.name, sum(e.amount)", f) == []
    assert "Unknown node label Invoice" in " ".join(lint("MATCH (i:Invoice) RETURN i", f))
    assert "ORDER BY" in error_hint("Invalid use of aggregating function max(...) in this context")


def test_reader_skips_sheets_that_are_not_tables(tmp_path):
    from openpyxl import Workbook

    wb = Workbook()
    wb.active.title = "ReadMe"
    wb.active.append(["Notes about this workbook"])
    ws = wb.create_sheet("Data")
    ws.append(["id", "name"])
    ws.append(["A1", "x"])
    wb.save(tmp_path / "w.xlsx")
    assert [s.name for s in read_table_file(tmp_path / "w.xlsx")] == ["Data"]
    only_notes = Workbook()
    only_notes.active.append(["just a note"])
    only_notes.save(tmp_path / "n.xlsx")
    with pytest.raises(TabularError, match="No table found"):
        read_table_file(tmp_path / "n.xlsx")
