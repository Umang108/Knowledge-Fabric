from types import SimpleNamespace

from app import extraction
from app.tabular import Sheet, profile_column


def make_sheet(name, columns, rows):
    sheet = Sheet(name, 1, columns, rows)
    sheet.profile = {column: profile_column(column, [row.get(column) for row in rows]) for column in columns}
    return sheet


def test_serializer_preserves_headers_rows_duplicates_unicode_and_nulls():
    sheet = make_sheet(
        "Events",
        ["source", "target", "weight", "note"],
        [
            {"source": "A", "target": "B", "weight": 1.5, "note": "café"},
            {"source": "A", "target": "B", "weight": 1.5, "note": None},
        ],
    )
    serialized = extraction.serialize_sheet(sheet)
    assert "SHEET: Events" in serialized
    assert "source | target | weight | note" in serialized
    assert serialized.count('"source":"A"') == 2
    assert "café" in serialized and '"note":null' in serialized


def test_full_data_discovery_supports_same_type_edges_and_properties(monkeypatch):
    people = make_sheet("People", ["person_key", "name"], [{"person_key": "P1", "name": "One"}])
    links = make_sheet(
        "Connections",
        ["from_key", "to_key", "weight", "status"],
        [{"from_key": "P1", "to_key": "P1", "weight": 0.8, "status": "active"}],
    )
    nodes = [
        {"label": "Person", "sheet": "People", "role": "row", "key": {"column": "person_key"}},
    ]
    settings = SimpleNamespace(full_sheet_llm_analysis=True, max_llm_rows=10, max_llm_chars=10_000)
    monkeypatch.setattr(extraction, "get_settings", lambda: settings)
    monkeypatch.setattr(
        extraction,
        "ask_json",
        lambda system, prompt: {
            "relationships": [
                {
                    "sheet": "Connections",
                    "from": {"label": "Person", "column": "from_key"},
                    "relationship": "DEPENDS_ON",
                    "to": {"label": "Person", "column": "to_key"},
                    "properties": ["weight", "status"],
                    "evidence": "The connection row records a directed edge and its weight/status.",
                    "confidence": 0.95,
                }
            ]
        },
    )
    found = extraction._discover_llm_links([people, links], nodes, [])
    assert len(found) == 1
    assert found[0]["a"] == found[0]["b"] == "Person"
    assert found[0]["suggested_type"] == "DEPENDS_ON"
    assert found[0]["properties"] == ["weight", "status"]


def test_duplicate_relationship_candidates_merge_evidence_and_properties():
    base = {
        "sheet": "Connections",
        "a": "Person",
        "a_column": "from_key",
        "b": "Person",
        "b_column": "to_key",
        "required": True,
    }
    merged = extraction._merge_links(
        [
            {**base, "properties": ["weight"], "evidence": {"reason": "value match"}},
            {**base, "properties": ["status"], "evidence": {"reason": "explicit link row"}},
        ]
    )
    assert len(merged) == 1
    assert merged[0]["properties"] == ["status", "weight"]
    assert "value match" in merged[0]["evidence"] and "explicit link row" in merged[0]["evidence"]


def test_relationship_naming_accepts_llm_evidence_without_legacy_why(monkeypatch):
    sheet = make_sheet(
        "Connections",
        ["from_key", "to_key"],
        [{"from_key": "P1", "to_key": "P2"}],
    )
    monkeypatch.setattr(extraction, "get_settings", lambda: SimpleNamespace(full_sheet_llm_analysis=False))
    monkeypatch.setattr(
        extraction,
        "ask_json",
        lambda system, prompt: {
            "relationships": [{"id": 1, "type": "KNOWS", "from": "Person", "to": "Person"}]
        },
    )
    links = [
        {
            "sheet": "Connections",
            "a": "Person",
            "a_column": "from_key",
            "b": "Person",
            "b_column": "to_key",
            "properties": [],
            "required": True,
            "evidence": {"reason": "Both columns contain person keys."},
        }
    ]

    relationships = extraction.name_links(links, ["Person"], [sheet])

    assert relationships[0]["type"] == "KNOWS"


def test_generic_relationship_sheet_is_not_promoted_to_a_node():
    customers = make_sheet(
        "Customers", ["Customer_ID", "Customer_Name"], [{"Customer_ID": "C1", "Customer_Name": "Acme"}]
    )
    products = make_sheet("Products", ["Product_ID", "Product_Name"], [{"Product_ID": "P1", "Product_Name": "Widget"}])
    relationships = make_sheet(
        "Relationships",
        ["Source", "Target", "Relationship"],
        [{"Source": "Acme", "Target": "Widget", "Relationship": "USES"}],
    )
    answers = {
        "Customers": {"row_entity": {"label": "Customer", "key_column": "Customer_ID"}},
        "Products": {"row_entity": {"label": "Product", "key_column": "Product_ID"}},
        "Relationships": {"row_entity": {"label": "Relationship", "key_column": "Source"}},
    }

    nodes, _ = extraction.build_nodes([customers, products, relationships], answers, {})

    assert {node["label"] for node in nodes} == {"Customer", "Product"}
