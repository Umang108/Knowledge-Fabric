"""Validation and mapping for PII decisions made on the graph review screen."""

from app import graph_schema as gs
from app import nist_pii

PII_CATEGORIES = list(nist_pii.CATEGORIES)
PII_STATUSES = ("detected", "confirmed", "dismissed")
PII_SOURCES = ("llm", "rules", "user")


def clean_pii(items: object, sheets: dict) -> list[dict]:
    """Validate the PII list edited on the Review screen; retain one entry per sheet and column.

    Dismissed decisions remain in the returned list for audit purposes. Invalid edits raise
    ``gs.SchemaError`` so callers can report them without persisting partial changes.
    """
    if not isinstance(items, list):
        raise gs.SchemaError(["pii must be a list"])
    out, errors = {}, []
    for i, item in enumerate(items, 1):
        if not isinstance(item, dict):
            errors.append(f"PII entry {i} is not an object")
            continue
        sheet, column = item.get("sheet"), item.get("column")
        if not isinstance(sheet, str) or sheet not in sheets:
            errors.append(f"PII entry {i}: unknown sheet {sheet!r}")
            continue
        if not isinstance(column, str) or column not in (sheets[sheet] or {}).get("columns", {}):
            errors.append(f"PII entry {i}: column {column!r} is not in sheet {sheet}")
            continue
        category = str(item.get("category") or "").strip().lower()
        if category not in PII_CATEGORIES:
            errors.append(f"PII on {sheet}.{column}: category must be one of {', '.join(PII_CATEGORIES)}")
            continue
        status = item.get("status") or "detected"
        if status not in PII_STATUSES:
            errors.append(f"PII on {sheet}.{column}: status must be one of {', '.join(PII_STATUSES)}")
            continue
        source = item.get("detected_by") if item.get("detected_by") in PII_SOURCES else "user"
        try:
            confidence = min(max(float(item.get("confidence", 1.0)), 0.0), 1.0)
        except (TypeError, ValueError):
            confidence = 1.0
        reason = str(item.get("reason") or ("Marked as PII on the Review screen" if source == "user" else ""))
        out[(sheet, column)] = {
            "sheet": sheet,
            "column": column,
            "category": category,
            "confidence": round(confidence, 3),
            "reason": reason[:300],
            "detected_by": source,
            "status": status,
        }
    if errors:
        raise gs.SchemaError(errors)
    return nist_pii.assess(list(out.values()), sheets)


def active_pii(schema: dict) -> list[dict]:
    """Return PII entries that have not been dismissed by a reviewer."""
    return [item for item in schema.get("pii", []) if item.get("status", "detected") != "dismissed"]


def pii_targets(schema: dict) -> list[dict]:
    """Map PII decisions to graph property names, retaining dismissed entries for audit."""
    out = []
    for item in schema.get("pii", []):
        item = {**item, "status": item.get("status", "detected")}
        for node in schema["nodes"]:
            if node["sheet"] != item["sheet"]:
                continue
            if node["key"]["column"] == item["column"]:
                out.append({**item, "node_label": node["label"], "property_name": node["key"]["name"]})
            for prop in node.get("properties", []):
                if prop["column"] == item["column"]:
                    out.append({**item, "node_label": node["label"], "property_name": prop["name"]})
        for relationship in schema["relationships"]:
            if relationship["sheet"] != item["sheet"]:
                continue
            for prop in relationship["properties"]:
                if prop["column"] == item["column"]:
                    out.append({**item, "node_label": relationship["type"], "property_name": prop["name"]})
    return out
