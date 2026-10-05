"""Graph schema: the structure the LLM proposes, the user edits on the Review screen and
the loader executes. Cypher is generated from it deterministically, so the preview always
matches what runs.

    {
      "source_file": "supplier_orders.xlsx",
      "sheets": {"Suppliers": {"header_row": 3, "rows": 50, "columns": {"Supplier ID": {...profile}}}},
      "nodes": [{"id": "n1", "label": "Supplier", "sheet": "Suppliers", "role": "row" | "embedded",
                 "key": {"name": "supplier_id", "column": "Supplier ID"},
                 "properties": [{"name": "name", "column": "Supplier Name", "type": "string"}],
                 "count": 48}],
      "relationships": [{"id": "r1", "type": "SUPPLIES", "sheet": "Products",
                         "from": {"label": "Supplier", "column": "Supplier"},
                         "to": {"label": "Product", "column": "SKU"},
                         "properties": [], "required": true, "count": 307}]
    }
"""

import copy
import re

LABEL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
REL_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
PROP_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
TYPES = {"string", "integer", "float", "date", "boolean"}


class SchemaError(ValueError):
    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


def to_snake(s: str) -> str:
    s = re.sub(r"\(.*?\)", "", str(s))  # "Rating (1-5)" -> "Rating"
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s)
    s = re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_").lower()
    if not s:
        s = "value"
    return s if s[0].isalpha() else f"p_{s}"


def to_pascal(s: str) -> str:
    parts = re.split(r"[^A-Za-z0-9]+", re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", str(s)))
    out = "".join(p[:1].upper() + p[1:].lower() for p in parts if p)
    if not out or not out[0].isalpha():
        out = f"Node{out}"
    return out


def singular(label: str) -> str:
    if len(label) > 3 and label.endswith("ies"):
        return label[:-3] + "y"
    if len(label) > 3 and label.endswith("s") and not label.endswith(("ss", "us", "is")):
        return label[:-1]
    return label


def to_rel_type(s: str) -> str:
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(s))
    s = re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_").upper()
    return s if s and s[0].isalpha() else f"REL_{s or 'LINK'}"


def node_by_label(schema: dict, label: str) -> dict | None:
    return next((n for n in schema["nodes"] if n["label"] == label), None)


def normalize(schema: dict) -> dict:
    """Fix naming so user edits can't produce invalid Cypher identifiers."""
    schema = copy.deepcopy(schema)
    renames = {}
    for i, n in enumerate(schema.get("nodes", []), 1):
        n.setdefault("id", f"n{i}")
        new = to_pascal(n["label"]) if not LABEL_RE.match(str(n.get("label", ""))) else n["label"]
        renames[n.get("label")] = new
        n["label"] = new
        n.setdefault("role", "row")
        n["key"]["name"] = to_snake(n["key"]["name"]) if not PROP_RE.match(n["key"]["name"]) else n["key"]["name"]
        for p in n.get("properties", []):
            p["name"] = to_snake(p["name"]) if not PROP_RE.match(p["name"]) else p["name"]
            p["type"] = p.get("type") if p.get("type") in TYPES else "string"
    for i, r in enumerate(schema.get("relationships", []), 1):
        r.setdefault("id", f"r{i}")
        r["type"] = to_rel_type(r["type"]) if not REL_RE.match(str(r.get("type", ""))) else r["type"]
        for end in ("from", "to"):
            r[end]["label"] = renames.get(r[end]["label"], r[end]["label"])
        r.setdefault("required", True)
        r.setdefault("properties", [])
        for p in r["properties"]:
            p["name"] = to_snake(p["name"]) if not PROP_RE.match(p["name"]) else p["name"]
            p["type"] = p.get("type") if p.get("type") in TYPES else "string"
    return schema


def validate(schema: dict) -> list[str]:
    errors = []
    sheets = schema.get("sheets", {})
    labels = [n["label"] for n in schema["nodes"]]
    for dup in {lab for lab in labels if labels.count(lab) > 1}:
        errors.append(f"Node type {dup} is defined more than once")
    if not schema["nodes"]:
        errors.append("At least one node type is required")
    for n in schema["nodes"]:
        where = f"Node {n['label']}"
        if n["label"].upper().startswith("KB_"):
            errors.append(f"{where}: labels starting with KB_ are reserved")
        cols = sheets.get(n["sheet"], {}).get("columns", {})
        if n["sheet"] not in sheets:
            errors.append(f"{where}: unknown sheet '{n['sheet']}'")
        if n["key"]["column"] not in cols:
            errors.append(f"{where}: key column '{n['key']['column']}' not in sheet {n['sheet']}")
        names = [n["key"]["name"]] + [p["name"] for p in n.get("properties", [])]
        for dup in {x for x in names if names.count(x) > 1}:
            errors.append(f"{where}: property '{dup}' appears more than once")
        for p in n.get("properties", []):
            if p["column"] not in cols:
                errors.append(f"{where}: column '{p['column']}' not in sheet {n['sheet']}")
    for r in schema["relationships"]:
        where = f"Relationship {r['type']}"
        cols = sheets.get(r["sheet"], {}).get("columns", {})
        if r["sheet"] not in sheets:
            errors.append(f"{where}: unknown sheet '{r['sheet']}'")
        for end in ("from", "to"):
            if r[end]["label"] not in labels:
                errors.append(f"{where}: node type '{r[end]['label']}' does not exist")
            if r[end]["column"] not in cols:
                errors.append(f"{where}: column '{r[end]['column']}' not in sheet {r['sheet']}")
        for p in r["properties"]:
            if p["column"] not in cols:
                errors.append(f"{where}: column '{p['column']}' not in sheet {r['sheet']}")
    return errors


def clean(schema: dict) -> dict:
    schema = normalize(schema)
    errors = validate(schema)
    if errors:
        raise SchemaError(errors)
    return schema


# ------------------------------------------------------------------ Cypher
def _labels(label: str, kb_label: str | None) -> str:
    return f"`{label}`:`{kb_label}`" if kb_label else f"`{label}`"


def node_cypher(node: dict, kb_label: str | None = None, prop_names: list[str] | None = None) -> str:
    props = [p["name"] for p in node.get("properties", []) if prop_names is None or p["name"] in prop_names]
    key = node["key"]["name"]
    q = f"UNWIND $rows AS row\nMERGE (n:{_labels(node['label'], kb_label)} {{{key}: row.{key}}})"
    if props:
        q += "\nSET " + ", ".join(f"n.{p} = row.{p}" for p in props)
    return q


def relationship_cypher(
    rel: dict, schema: dict, kb_label: str | None = None, prop_names: list[str] | None = None
) -> str:
    a, b = node_by_label(schema, rel["from"]["label"]), node_by_label(schema, rel["to"]["label"])
    props = [p["name"] for p in rel["properties"] if prop_names is None or p["name"] in prop_names]
    q = (
        f"UNWIND $rows AS row\n"
        f"MATCH (a:{_labels(a['label'], kb_label)} {{{a['key']['name']}: row.from_key}})\n"
        f"MATCH (b:{_labels(b['label'], kb_label)} {{{b['key']['name']}: row.to_key}})\n"
        f"MERGE (a)-[r:`{rel['type']}`]->(b)"
    )
    if props:
        q += "\nSET " + ", ".join(f"r.{p} = row.{p}" for p in props)
    return q


def index_cypher(node: dict) -> str:
    return (
        f"CREATE INDEX `idx_{node['label']}_{node['key']['name']}` IF NOT EXISTS "
        f"FOR (n:`{node['label']}`) ON (n.`{node['key']['name']}`)"
    )


def preview(schema: dict) -> list[str]:
    """Readable Cypher for the Review screen (single-line statements, one per node/relationship type)."""
    out = []
    for n in schema["nodes"]:
        key = n["key"]["name"]
        sets = ", ".join(f"n.{p['name']} = row.{p['name']}" for p in n.get("properties", []))
        out.append(f"MERGE (n:{n['label']} {{{key}: row.{key}}})" + (f" SET {sets}" if sets else ""))
    for r in schema["relationships"]:
        a, b = node_by_label(schema, r["from"]["label"]), node_by_label(schema, r["to"]["label"])
        if not a or not b:
            continue
        sets = ", ".join(f"r.{p['name']} = row.{p['name']}" for p in r["properties"])
        out.append(
            f"MATCH (a:{a['label']} {{{a['key']['name']}: row.from_key}}), "
            f"(b:{b['label']} {{{b['key']['name']}: row.to_key}}) "
            f"MERGE (a)-[r:{r['type']}]->(b)" + (f" SET {sets}" if sets else "")
        )
    return out


def summary(schema: dict) -> dict:
    return {
        "node_types": len(schema["nodes"]),
        "entities": sum(n.get("count") or 0 for n in schema["nodes"]),
        "relationship_types": len({r["type"] for r in schema["relationships"]}),
    }


def describe_for_llm(schema: dict, value_hints: dict | None = None) -> str:
    """Compact schema text for the chat Cypher generator (no internal labels)."""
    value_hints = value_hints or {}
    lines = ["Node labels and properties:"]
    for n in schema["nodes"]:
        ids = value_hints.get((n["label"], n["key"]["name"]))
        props = [
            f"{n['key']['name']} (string, unique id" + (f", e.g. {', '.join(map(repr, ids))}" if ids else "") + ")"
        ]
        for p in n.get("properties", []):
            hint = value_hints.get((n["label"], p["name"]))
            props.append(
                f"{p['name']} ({p['type']}" + (f"; values: {', '.join(map(repr, hint))}" if hint else "") + ")"
            )
        lines.append(f"- {n['label']}: " + ", ".join(props))
    lines.append("Relationships (direction matters):")
    for r in schema["relationships"]:
        props = ", ".join(f"{p['name']} ({p['type']})" for p in r["properties"])
        lines.append(
            f"- (:{r['from']['label']})-[:{r['type']}]->(:{r['to']['label']})"
            + (f" relationship properties: {props}" if props else "")
        )
    return "\n".join(lines)
