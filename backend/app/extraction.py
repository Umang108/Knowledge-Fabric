"""LLM extraction of a graph schema (and PII columns) from a CSV/XLSX file.

The LLM makes the modelling decisions (what a row is, which columns identify it, which
entities are embedded, relationship names/direction, PII). Deterministic profiling gives it
evidence and checks its answers: value overlap finds references between sheets, and a
functional-dependency test finds line-level columns (e.g. qty on an order line) that belong on
a relationship rather than on the node. Everything the LLM says is validated; anything unusable
falls back to a heuristic so extraction always produces an editable draft.
"""

import json
import logging
import re
from collections import Counter, defaultdict

from app import graph_schema as gs
from app import nist_pii, pii_detection, pii_review
from app.config import get_settings
from app.llm import ask_json
from app.tabular import Sheet, coerce, is_blank, norm_key

log = logging.getLogger(__name__)

PII_CATEGORIES = pii_review.PII_CATEGORIES
PII_SOURCES = pii_review.PII_SOURCES
PII_STATUSES = pii_review.PII_STATUSES
PII_SYSTEM = pii_detection.PII_SYSTEM
PII_PROMPT = pii_detection.PII_PROMPT
active_pii = pii_review.active_pii
clean_pii = pii_review.clean_pii
pii_targets = pii_review.pii_targets
LINK_OVERLAP = 0.6
UNIQUE = 0.95  # distinct ratio treated as unique (tolerates a few duplicate rows)


# ------------------------------------------------------------------ prompts
def _column_lines(sheet: Sheet) -> str:
    lines = []
    for c in sheet.profile.values():
        samples = ", ".join(c.samples[:4])
        lines.append(f'- "{c.name}": {c.type}, {c.fill_rate:.0%} filled, {c.distinct} distinct values; e.g. {samples}')
    return "\n".join(lines)


def serialize_sheet(sheet: Sheet, rows: list[dict] | None = None) -> str:
    """Serialize a sheet with its headers and rows without dropping cell values."""
    rows = sheet.rows if rows is None else rows
    return (
        f"SHEET: {sheet.name}\n\n"
        f"COLUMNS:\n{' | '.join(sheet.columns)}\n\n"
        "ROWS:\n"
        + "\n".join(json.dumps(row, ensure_ascii=False, default=str, separators=(",", ":")) for row in rows)
    )


def _sheet_chunks(sheet: Sheet) -> list[str]:
    settings = get_settings()
    chunks, rows = [], []
    for row in sheet.rows:
        candidate = rows + [row]
        serialized = serialize_sheet(sheet, candidate)
        if rows and (len(candidate) > settings.max_llm_rows or len(serialized) > settings.max_llm_chars):
            chunks.append(serialize_sheet(sheet, rows))
            rows = [row]
        else:
            rows = candidate
    if rows or not chunks:
        chunks.append(serialize_sheet(sheet, rows))
    return chunks


def _workbook_chunks(sheets: list[Sheet]) -> list[str]:
    settings = get_settings()
    chunks, current = [], ""
    for sheet in sheets:
        for part in _sheet_chunks(sheet):
            if current and len(current) + len(part) + 2 > settings.max_llm_chars:
                chunks.append(current)
                current = ""
            current = f"{current}\n\n{part}".strip()
    if current or not chunks:
        chunks.append(current)
    return chunks


def _profile_data(sheet: Sheet) -> str:
    return f"SHEET: {sheet.name}\n\nCOLUMNS:\n{_column_lines(sheet)}"


def _analysis_data(sheet: Sheet) -> list[str]:
    return _sheet_chunks(sheet) if get_settings().full_sheet_llm_analysis else [_profile_data(sheet)]


ENTITY_SYSTEM = """You are a data modeller who designs Neo4j property graphs from spreadsheets.
Reply with a single JSON object and nothing else."""

ENTITY_PROMPT = """Workbook "{file}" has these sheets: {sheets}.

Sheet "{sheet}" has {rows} rows. Columns (type, fill, distinct values, examples):
{columns}
{hints}

{data_instruction}

Decide what one row of sheet "{sheet}" describes.
- "row_entity": the thing each row describes, as a singular PascalCase label (e.g. Employee, Invoice, Patient),
  its identifying column ("key_column", copied exactly), and its descriptive columns ("property_columns").
  Use null if every row only links things described in other sheets (a link/junction table, e.g. which
  student is enrolled in which course).
- "reference_columns": columns holding the ID of something described in ANOTHER sheet
  (e.g. department_id in an employees sheet).
- "embedded_entities": things that have no sheet of their own but are named in a column here and are worth
  their own node (e.g. a Manager column in a projects sheet). Each needs "label", "key_column",
  "property_columns". Do not list things that have their own sheet.
- "ignore_columns": row numbers, running indexes, empty or junk columns.

Use column names exactly as written above. JSON shape:
{{"row_entity": {{"label": "...", "key_column": "...", "property_columns": ["..."]}} | null,
  "reference_columns": ["..."],
  "embedded_entities": [{{"label": "...", "key_column": "...", "property_columns": ["..."]}}],
  "ignore_columns": ["..."]}}"""

REL_PROMPT = """Graph node types: {labels}.

These pairs of node types are linked in the data:
{links}

{data_instruction}

For each link write a short English sentence with the subject first (e.g. "Employee works in Department",
"Doctor treats Patient", "Invoice issued by Vendor"), then the relationship type in UPPER_SNAKE_CASE (the verb,
e.g. WORKS_IN, TREATS, ISSUED_BY), "from" = the
subject node type and "to" = the object node type. Both must be the two node types of that link.
JSON shape: {{"relationships": [{{"id": 1, "sentence": "...", "type": "...", "from": "...", "to": "..."}}]}}"""

DISCOVERY_PROMPT = """You are discovering a domain-independent property graph from a complete spreadsheet workbook.
Analyze the actual rows, not only headers or profiles. Find every defensible relationship, including:
cross-sheet references, explicit relationship/link tables, repeated associations, hierarchical records,
indirect relationships, relationship records, and relationships whose source and target have the same type.
Do not invent an edge without evidence in the supplied rows. Preserve relationship-level columns as properties.

Known candidate node types:
{nodes}

{data}

Return one JSON object. Use exact sheet and column names from the data:
{{"relationships": [{{
    "sheet": "...",
    "from": {{"label": "...", "column": "..."}},
    "relationship": "MEANINGFUL_UPPER_SNAKE_CASE",
    "to": {{"label": "...", "column": "..."}},
    "properties": ["column name"],
    "evidence": "brief explanation grounded in the rows",
    "confidence": 0.0
}}]}}"""

# ------------------------------------------------------------------ profiling helpers
_ID_NAME = re.compile(
    r"(^|[^a-z])(id|code|sku|no|number|key|ref|reg|uhid|mrn|uid|uuid|guid)([^a-z]|$)", re.I
)
_EMBEDDED_COLUMN = re.compile(
    r"^(insurer|manager|supervisor|vendor|approver|department|doctor|employee|project|customer|supplier|"
    r"warehouse|product|medication|procedure|ward)(?:\s+(?:id|code|name|reg|no))?$",
    re.I,
)
_GENERIC_LINK_LABELS = {
    "link",
    "links",
    "mapping",
    "mappings",
    "junction",
    "junctions",
    "bridge",
    "bridges",
    "relation",
    "relations",
    "relationship",
    "relationships",
}


def _names_entity(sheet: Sheet, column: str, label: str | None = None) -> bool:
    """The column is named after the thing a row describes ("SalesOrder" in sheet "SalesOrders"): ERP exports
    (SAP OData, ...) name their numeric document numbers like that instead of "..._id"."""
    col = re.sub(r"[^a-z0-9]", "", column.lower())
    names = {sheet.name, gs.singular(gs.to_pascal(sheet.name))} | ({label} if label else set())
    return any(col == re.sub(r"[^a-z0-9]", "", n.lower()) for n in names if n)


def _key_ok(sheet: Sheet, column: str, label: str | None = None) -> bool:
    """Keys are text (or integers named like IDs); a quantity or a date can't identify anything."""
    p = sheet.profile[column]
    if p.fill_rate < 0.9:
        return False
    if p.type == "string":
        return True
    # integer codes (GL account 5100) are fine; measures (stock_qty) have many distinct values
    return p.type == "integer" and (
        bool(_ID_NAME.search(column))
        or (p.distinct <= 100 and p.distinct_ratio <= 0.2)
        or (p.id_like and _names_entity(sheet, column, label))
    )


def _heuristic_key(sheet: Sheet) -> str:
    for c in sheet.columns:
        p = sheet.profile[c]
        if (_ID_NAME.search(c) or _names_entity(sheet, c)) and _key_ok(sheet, c) and p.distinct_ratio > 0.3:
            return c
    candidates = [c for c in sheet.columns if _key_ok(sheet, c)] or sheet.columns
    return max(candidates, key=lambda c: (sheet.profile[c].distinct_ratio, sheet.profile[c].fill_rate))


def _fix_column(name, sheet: Sheet) -> str | None:
    if not isinstance(name, str):
        return None
    if name in sheet.columns:
        return name
    wanted = re.sub(r"[^a-z0-9]", "", name.lower())
    for c in sheet.columns:
        if re.sub(r"[^a-z0-9]", "", c.lower()) == wanted:
            return c
    return None


def _columns(names, sheet: Sheet) -> list[str]:
    out = []
    for n in names or []:
        c = _fix_column(n, sheet)
        if c and c not in out:
            out.append(c)
    return out


def _line_level_columns(sheet: Sheet, key_col: str) -> set[str]:
    """Columns whose value varies between rows with the same key (the sheet is at line level)."""
    groups = defaultdict(list)
    for r in sheet.rows:
        k = norm_key(r.get(key_col))
        if k:
            groups[k].append(r)
    multi = [g for g in groups.values() if len(g) > 1]
    if not multi:
        return set()
    varying = set()
    for c in sheet.columns:
        if c == key_col:
            continue
        t = sheet.profile[c].type  # compare parsed values: one date written four ways is still one date
        inconsistent = sum(len({norm_key(coerce(r.get(c), t)) for r in g}) > 1 for g in multi)
        if inconsistent > 0.05 * len(multi):
            varying.add(c)
    return varying


def _looks_junk(sheet: Sheet, column: str) -> bool:
    """Unnamed, essentially empty, or a running row counter. Sparse free text (notes) is kept."""
    p = sheet.profile[column]
    if column.startswith("column_") or p.fill_rate < 0.005:
        return True
    if p.type == "integer" and p.distinct_ratio >= 0.99:
        vals = [v for v in sheet.values(column) if isinstance(v, int)]
        return len(vals) > 10 and all(b - a == 1 for a, b in zip(vals, vals[1:], strict=False))
    return False


def _heuristic_entity(sheet: Sheet) -> dict:
    return {"label": gs.singular(gs.to_pascal(sheet.name)), "key_column": _heuristic_key(sheet), "property_columns": []}


def _heuristic_embedded(sheet: Sheet, row: dict | None) -> list[dict]:
    """Recover obvious embedded entity columns when the model provides no answer."""
    if get_settings().full_sheet_llm_analysis:
        return []
    if not row:
        return []
    entities = []
    for column in sheet.columns:
        if (
            column == row["key_column"]
            or not _EMBEDDED_COLUMN.fullmatch(column.strip())
            or _ID_NAME.search(column)
            or sheet.profile[column].type != "string"
            or sheet.profile[column].fill_rate < 0.5
            or sheet.profile[column].distinct_ratio >= 0.5
        ):
            continue
        label = gs.singular(gs.to_pascal(column))
        if label == row["label"]:
            continue
        entities.append({"label": label, "key_column": column, "property_columns": []})
    return entities


def _generic_link_entity(sheet: Sheet, row: dict, evidence: set[str]) -> bool:
    """A generic relationship row with multiple references is a junction, not a node type."""
    sheet_name = re.sub(r"[^a-z0-9]", "", sheet.name.lower())
    label = re.sub(r"[^a-z0-9]", "", row["label"].lower())
    generic_name = sheet_name in _GENERIC_LINK_LABELS or label in _GENERIC_LINK_LABELS
    return generic_name and (len(evidence) >= 2 or len(sheet.columns) >= 3)


def _reason(link: dict) -> str:
    evidence = link.get("evidence", "")
    return evidence.get("reason", "") if isinstance(evidence, dict) else evidence


def _merge_links(links: list[dict]) -> list[dict]:
    merged = {}
    for link in links:
        key = (
            link["sheet"],
            link["a"],
            link["a_column"],
            link["b"],
            link["b_column"],
        )
        if key not in merged:
            merged[key] = {**link, "properties": list(link.get("properties", []))}
            continue
        current = merged[key]
        current["properties"] = sorted(set(current.get("properties", [])) | set(link.get("properties", [])))
        current["evidence"] = "; ".join(x for x in (_reason(current), _reason(link)) if x)
        if link.get("suggested_type") and not current.get("suggested_type"):
            current["suggested_type"] = link["suggested_type"]
    return list(merged.values())


def _discover_llm_links(sheets: list[Sheet], nodes: list[dict], deterministic: list[dict]) -> list[dict]:
    if not get_settings().full_sheet_llm_analysis:
        return []
    sheet_map = {s.name: s for s in sheets}
    labels = ", ".join(f"{n['label']} [{n['sheet']}.{n['key']['column']}]" for n in nodes)
    existing = "\n".join(
        f"- {r['sheet']}: {r['a']}({r['a_column']}) -> {r['b']}({r['b_column']})" for r in deterministic
    ) or "- none"
    found = []
    for data in _workbook_chunks(sheets):
        try:
            response = ask_json(
                ENTITY_SYSTEM,
                DISCOVERY_PROMPT.format(nodes=labels, data=data)
                + f"\nDeterministic candidates already found; do not duplicate them:\n{existing}",
            )
        except Exception as exc:
            log.warning("full-data relationship discovery failed: %s", exc)
            continue
        for item in response.get("relationships") or []:
            if not isinstance(item, dict):
                continue
            sheet_name = item.get("sheet")
            if sheet_name not in sheet_map:
                continue
            source, target = item.get("from") or {}, item.get("to") or {}
            source_node = next((n for n in nodes if n["label"] == source.get("label")), None)
            target_node = next((n for n in nodes if n["label"] == target.get("label")), None)
            if not source_node or not target_node:
                continue
            source_column = _fix_column(source.get("column"), sheet_map[sheet_name])
            target_column = _fix_column(target.get("column"), sheet_map[sheet_name])
            if not source_column or not target_column:
                continue
            properties = [
                column
                for column in _columns(item.get("properties"), sheet_map[sheet_name])
                if column not in (source_column, target_column)
            ]
            found.append(
                {
                    "sheet": sheet_name,
                    "a": source_node["label"],
                    "a_column": source_column,
                    "b": target_node["label"],
                    "b_column": target_column,
                    "properties": properties,
                    "suggested_type": str(item.get("relationship") or "").strip(),
                    "evidence": {"reason": str(item.get("evidence") or "full-data LLM analysis")},
                    "required": True,
                }
            )
    return _merge_links(found)


def key_values(sheet: Sheet, column: str) -> set[str]:
    return {k for k in (norm_key(v) for v in sheet.values(column)) if k}


def _name_match(column: str, label: str) -> bool:
    column_text = re.sub(r"[^a-z0-9]", "", column.lower())
    label_text = re.sub(r"[^a-z0-9]", "", label.lower())
    return bool(label_text and (label_text in column_text or column_text.startswith(label_text)))


def _link_evidence(sheet: Sheet, column: str, node: dict, target_sheet: Sheet) -> dict:
    source = key_values(sheet, column)
    target = key_values(target_sheet, node["key"]["column"])
    matched = source & target
    overlap = len(matched) / len(source) if source else 0.0
    reverse = len(matched) / len(target) if target else 0.0
    name_match = _name_match(column, node["label"])
    type_match = sheet.profile[column].type == target_sheet.profile[node["key"]["column"]].type
    confidence = min(1.0, 0.65 * overlap + 0.2 * reverse + 0.1 * name_match + 0.05 * type_match)
    return {
        "source_values": len(source),
        "target_values": len(target),
        "matched_values": len(matched),
        "overlap": round(overlap, 4),
        "reverse_overlap": round(reverse, 4),
        "name_match": name_match,
        "type_match": type_match,
        "confidence": round(confidence, 4),
    }


# ------------------------------------------------------------------ step 2: entities per sheet
def reference_evidence(sheets: list[Sheet]) -> dict[str, set[str]]:
    """Columns (per sheet) whose values are the IDs of another sheet's rows."""
    return {
        name: {h.split('"')[1] for h in hints if " holds IDs from sheet " in h}
        for name, hints in reference_hints(sheets).items()
    }


def reference_hints(sheets: list[Sheet]) -> dict[str, list[str]]:
    """Evidence for the prompt: columns whose values are the IDs of another sheet's rows."""

    def unique_cols(s):
        return [
            c
            for c in s.columns
            if s.profile[c].type in ("string", "integer")
            and _key_ok(s, c)
            and s.profile[c].distinct_ratio >= UNIQUE
            and s.profile[c].fill_rate > 0.9
        ]

    keys = {}
    for s in sheets:
        cands = unique_cols(s)
        if cands:
            k = next((c for c in cands if re.search(r"id|code|sku|no\b|number|key", c, re.I)), cands[0])
            keys[s.name] = (k, key_values(s, k))
    hints = {s.name: [] for s in sheets}
    for s in sheets:
        for c in s.columns:
            vals = key_values(s, c)
            if not vals or s.profile[c].type not in ("string", "integer"):
                continue
            for other in sheets:
                if other.name == s.name or other.name not in keys:
                    continue
                okey, ovals = keys[other.name]
                if len(vals & ovals) / len(vals) >= LINK_OVERLAP:
                    hints[s.name].append(f'"{c}" holds IDs from sheet "{other.name}" (column "{okey}")')
    uniq = {s.name: unique_cols(s) for s in sheets}
    for s in sheets:
        if uniq[s.name]:
            hints[s.name].append("Columns unique on every row: " + ", ".join(f'"{c}"' for c in uniq[s.name][:4]))
        else:
            hints[s.name].append(
                "No column is unique on every row, so several rows can describe the same thing "
                "(e.g. line items of one invoice) or each row links other things."
            )
    return hints


def ask_sheet_entities(sheet: Sheet, file_name: str, sheet_names: list[str], hints: list[str] | None = None) -> dict:
    hint_text = ("Evidence from the data:\n" + "\n".join(f"- {h}" for h in hints) + "\n") if hints else ""
    answers = []
    for data in _analysis_data(sheet):
        data_instruction = (
            "Complete sheet data. Analyze every supplied row; do not infer the schema only from metadata:\n" + data
            if get_settings().full_sheet_llm_analysis
            else "Profile-only analysis is enabled. Use the supplied column evidence and samples:\n" + data
        )
        prompt = ENTITY_PROMPT.format(
            file=file_name,
            sheets=", ".join(sheet_names),
            sheet=sheet.name,
            rows=len(sheet.rows),
            columns=_column_lines(sheet),
            hints=hint_text,
            data_instruction=data_instruction,
        )
        try:
            answers.append(ask_json(ENTITY_SYSTEM, prompt))
        except Exception as exc:  # the draft must still be produced; the reviewer fixes it
            log.warning("entity extraction failed for sheet %s: %s", sheet.name, exc)
    if not answers:
        return {}
    if len(answers) == 1:
        return answers[0]
    rows = [a.get("row_entity") for a in answers if isinstance(a.get("row_entity"), dict)]

    def row_key(x):
        return (str(x.get("label", "")), str(x.get("key_column", "")))

    row = Counter(row_key(item) for item in rows).most_common(1)
    chosen = next((item for item in rows if row and row[0][0] == row_key(item)), None)
    merged = {
        "row_entity": chosen,
        "reference_columns": sorted({c for a in answers for c in (a.get("reference_columns") or [])}),
        "ignore_columns": sorted({c for a in answers for c in (a.get("ignore_columns") or [])}),
    }
    embedded = {}
    for answer in answers:
        for item in answer.get("embedded_entities") or []:
            if isinstance(item, dict):
                embedded.setdefault(row_key(item), item)
    merged["embedded_entities"] = list(embedded.values())
    return merged


def _clean_entity(raw, sheet: Sheet) -> dict | None:
    if not isinstance(raw, dict):
        return None
    key = _fix_column(raw.get("key_column"), sheet)
    label = raw.get("label")
    if not key or not isinstance(label, str) or not label.strip() or not _key_ok(sheet, key, label):
        return None
    return {
        "label": gs.singular(gs.to_pascal(label)),
        "key_column": key,
        "property_columns": [c for c in _columns(raw.get("property_columns"), sheet) if c != key],
    }


def _entity_from_id_column(sheet: Sheet, refs: set[str]) -> dict | None:
    """A sheet without a row entity may still carry its own ID column (order_id on order lines)."""
    for c in sheet.columns:
        if c in refs or not _ID_NAME.search(c) or not _key_ok(sheet, c) or not sheet.profile[c].id_like:
            continue
        base = re.sub(r"[\s_-]*(id|code|no|number|key|ref)$", "", c, flags=re.I).strip() or sheet.name
        return {"label": gs.singular(gs.to_pascal(base)), "key_column": c, "property_columns": []}
    return None


def build_nodes(
    sheets: list[Sheet], answers: dict[str, dict], evidence: dict[str, set] | None = None
) -> tuple[list[dict], dict]:
    """Turn per-sheet LLM answers into node definitions; resolve duplicates across sheets."""
    evidence = evidence if evidence is not None else reference_evidence(sheets)
    per_sheet = {}
    for s in sheets:
        a = answers.get(s.name) or {}
        row = _clean_entity(a.get("row_entity"), s)
        # a link table needs at least two columns that reference other sheets
        is_link = a.get("row_entity", "missing") is None and len(evidence.get(s.name, ())) >= 2
        forced_link = bool(row and _generic_link_entity(s, row, evidence.get(s.name, set())))
        forced_key = row["key_column"] if forced_link else None
        if forced_link:
            row = None
            is_link = True
        if row is None and not is_link:  # unusable answer: fall back to a heuristic
            row = _heuristic_entity(s)
        embedded = [e for e in (_clean_entity(x, s) for x in (a.get("embedded_entities") or [])) if e]
        known_embedded = {e["label"] for e in embedded}
        embedded.extend(e for e in _heuristic_embedded(s, row) if e["label"] not in known_embedded)
        # only drop columns that also look like junk; small models over-use "ignore"
        ignore = {c for c in _columns(a.get("ignore_columns"), s) if _looks_junk(s, c)}
        ignore |= {c for c in s.columns if _looks_junk(s, c)}
        if forced_key:
            ignore.add(forced_key)
        per_sheet[s.name] = {
            "row": row,
            "embedded": embedded,
            "ignore": ignore,
            "refs": set(_columns(a.get("reference_columns"), s)),
            "forced_link": forced_link,
        }

    # A label that is the row entity of several sheets stays with the sheet where its key is most unique;
    # the other sheets become link tables / references.
    by_label = defaultdict(list)
    sheet_map = {s.name: s for s in sheets}
    for name, info in per_sheet.items():
        if info["row"]:
            by_label[info["row"]["label"]].append(name)
    for label, names in by_label.items():
        if len(names) > 1:
            keep = max(
                names,
                key=lambda n: (
                    sheet_map[n].profile[per_sheet[n]["row"]["key_column"]].distinct_ratio,
                    n.lower().startswith(label.lower()[:4]),
                ),
            )
            for n in names:
                if n != keep:  # the model reused a label; model this sheet from its own name instead
                    per_sheet[n]["refs"].add(per_sheet[n]["row"]["key_column"])
                    fallback = _heuristic_entity(sheet_map[n])
                    taken = {i["row"]["label"] for m, i in per_sheet.items() if i["row"] and m != n}
                    per_sheet[n]["row"] = fallback if fallback["label"] not in taken else None
    # A sheet whose "key" is really a reference to another sheet's entity (and isn't unique) is a
    # link table, e.g. Inventory keyed by sku.
    row_keys = {n: key_values(sheet_map[n], info["row"]["key_column"]) for n, info in per_sheet.items() if info["row"]}
    for name, info in per_sheet.items():
        if not info["row"]:
            continue
        key_col = info["row"]["key_column"]
        if sheet_map[name].profile[key_col].distinct_ratio >= UNIQUE:
            continue
        mine = row_keys[name]
        for other, keys in row_keys.items():
            if (
                other != name
                and mine
                and per_sheet[other]["row"]
                and len(mine & keys) / len(mine) >= LINK_OVERLAP
                and sheet_map[other].profile[per_sheet[other]["row"]["key_column"]].distinct_ratio >= UNIQUE
            ):
                info["refs"].add(key_col)
                info["row"] = None
                break
    taken = {info["row"]["label"] for info in per_sheet.values() if info["row"]}
    for name, info in per_sheet.items():
        if info["row"] is None and not info["forced_link"]:
            e = _entity_from_id_column(sheet_map[name], evidence.get(name, set()) | info["refs"])
            if e and e["label"] not in taken:
                info["row"] = e
                taken.add(e["label"])
    row_labels = {info["row"]["label"] for info in per_sheet.values() if info["row"]}
    # Embedded entities that have their own sheet are references, whether the model reused that sheet's label
    # or invented a new one (a "Dept" column holding Department codes).
    own_keys = {
        name: key_values(sheet_map[name], info["row"]["key_column"])
        for name, info in per_sheet.items()
        if info["row"] and sheet_map[name].profile[info["row"]["key_column"]].distinct_ratio >= UNIQUE
    }

    def is_reference(sheet_name, entity):
        vals = key_values(sheet_map[sheet_name], entity["key_column"])
        return bool(vals) and any(
            other != sheet_name and len(vals & keys) / len(vals) >= LINK_OVERLAP for other, keys in own_keys.items()
        )

    for name, info in per_sheet.items():
        kept = []
        for e in info["embedded"]:
            if e["label"] in row_labels or is_reference(name, e):
                info["refs"].add(e["key_column"])
            else:
                kept.append(e)
        info["embedded"] = kept
        seen = set()
        info["embedded"] = [e for e in info["embedded"] if not (e["label"] in seen or seen.add(e["label"]))]

    nodes, emb_seen = [], set()
    for s in sheets:
        info = per_sheet[s.name]
        if info["row"]:
            r = info["row"]
            nodes.append(
                {
                    "label": r["label"],
                    "sheet": s.name,
                    "role": "row",
                    "key": {"name": gs.to_snake(r["key_column"]), "column": r["key_column"]},
                    "properties": [],
                    "_llm_props": r["property_columns"],
                    "count": len(key_values(s, r["key_column"])),
                }
            )
        for e in info["embedded"]:
            if e["label"] in emb_seen:
                continue
            emb_seen.add(e["label"])
            nodes.append(
                {
                    "label": e["label"],
                    "sheet": s.name,
                    "role": "embedded",
                    "key": {"name": gs.to_snake(e["key_column"]), "column": e["key_column"]},
                    "properties": [
                        {"name": gs.to_snake(c), "column": c, "type": s.profile[c].type} for c in e["property_columns"]
                    ],
                    "count": len(key_values(s, e["key_column"])),
                }
            )
    return nodes, per_sheet


# ------------------------------------------------------------------ step 3: relationships
def detect_links(sheets: list[Sheet], nodes: list[dict], per_sheet: dict) -> list[dict]:
    """Find references between node types from value overlap; assign columns to nodes/relationships."""
    sheet_map = {s.name: s for s in sheets}
    key_nodes = {n["label"]: n for n in nodes}
    links = []
    for s in sheets:
        info = per_sheet[s.name]
        row = next((n for n in nodes if n["sheet"] == s.name and n["role"] == "row"), None)
        embedded = [n for n in nodes if n["sheet"] == s.name and n["role"] == "embedded"]
        used = set(info["ignore"])
        for e in embedded:
            used |= {e["key"]["column"]} | {p["column"] for p in e["properties"]}
        if row:
            used.add(row["key"]["column"])

        fks = []  # (column, label, evidence)
        for c in s.columns:
            if c in used or s.profile[c].type in ("boolean", "date", "float"):
                continue
            vals = key_values(s, c)
            if not vals:
                continue
            candidates = []
            for label, node in key_nodes.items():
                target_sheet = sheet_map[node["sheet"]]
                if row and label == row["label"] and c == row["key"]["column"]:
                    continue
                evidence = _link_evidence(s, c, node, target_sheet)
                if evidence["overlap"] >= LINK_OVERLAP or (
                    evidence["overlap"] >= 0.35 and evidence["name_match"] and evidence["type_match"]
                ):
                    candidates.append((evidence["confidence"], label, evidence))
            if candidates:
                _, label, evidence = max(candidates, key=lambda item: item[0])
                fks.append((c, label, evidence))
        fk_cols = {c for c, _, _ in fks}
        free = [c for c in s.columns if c not in used and c not in fk_cols]

        if row:
            line_cols = (
                _line_level_columns(s, row["key"]["column"])
                if s.profile[row["key"]["column"]].distinct_ratio < UNIQUE
                else set()
            )
            line_fk = next((c for c, _, _ in fks if c in line_cols), None)
            line_props = [c for c in free if c in line_cols] if line_fk else []
            node_cols = [c for c in free if c not in line_props]
            llm_props = [c for c in row.pop("_llm_props", []) if c in node_cols]
            ordered = llm_props + [c for c in node_cols if c not in llm_props]
            row["properties"] = [{"name": gs.to_snake(c), "column": c, "type": s.profile[c].type} for c in ordered]
            for c, label, evidence in fks:
                links.append(
                    {
                        "sheet": s.name,
                        "a": row["label"],
                        "a_column": row["key"]["column"],
                        "b": label,
                        "b_column": c,
                        "required": s.profile[c].fill_rate >= 0.9,
                        "properties": line_props if c == line_fk else [],
                        "evidence": evidence,
                        "why": f"{row['label']} rows reference {label} through column '{c}' in sheet '{s.name}'",
                    }
                )
            for e in embedded:
                links.append(
                    {
                        "sheet": s.name,
                        "a": row["label"],
                        "a_column": row["key"]["column"],
                        "b": e["label"],
                        "b_column": e["key"]["column"],
                        "required": s.profile[e["key"]["column"]].fill_rate >= 0.9,
                        "properties": [],
                        "why": f"each {row['label']} row names a {e['label']} in column '{e['key']['column']}'",
                    }
                )
        elif len(fks) >= 2:
            # A junction row can connect more than two entities. Keep the first
            # reference as the anchor and emit an edge for every other reference.
            c1, l1, _ = fks[0]
            for c2, l2, evidence in fks[1:]:
                links.append(
                    {
                        "sheet": s.name,
                        "a": l1,
                        "a_column": c1,
                        "b": l2,
                        "b_column": c2,
                        "required": True,
                        "properties": free,
                        "evidence": evidence,
                        "why": f"sheet '{s.name}' links {l1} ('{c1}') to {l2} ('{c2}')"
                        + (f" with {', '.join(free)}" if free else ""),
                    }
                )
        elif fks:  # a "link table" with one reference: keep its data as a node after all
            c, label, evidence = fks[0]
            key = _heuristic_key(s)
            n = {
                "label": gs.singular(gs.to_pascal(s.name)),
                "sheet": s.name,
                "role": "row",
                "key": {"name": gs.to_snake(key), "column": key},
                "properties": [
                    {"name": gs.to_snake(x), "column": x, "type": s.profile[x].type} for x in free if x != key
                ],
                "count": len(key_values(s, key)),
            }
            nodes.append(n)
            links.append(
                {
                    "sheet": s.name,
                    "a": n["label"],
                    "a_column": key,
                    "b": label,
                    "b_column": c,
                    "required": s.profile[c].fill_rate >= 0.9,
                    "properties": [],
                    "evidence": evidence,
                    "why": f"{n['label']} rows reference {label} through column '{c}'",
                }
            )
    for n in nodes:
        n.pop("_llm_props", None)
    return links


def _short_rel(raw) -> str | None:
    """Model names like PLACED_BY_CUSTOMER_TO_SHOP -> PLACED; STORED_IN_THE_WAREHOUSE -> STORED_IN."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    parts = gs.to_rel_type(raw).split("_")
    if len(parts) > 3 or len("_".join(parts)) > 24:
        keep2 = len(parts) > 1 and (parts[0] in ("HAS", "IS") or parts[1] in _PREPOSITIONS)
        parts = parts[:2] if keep2 else parts[:1]
    return "_".join(parts)


_PREPOSITIONS = {"IN", "TO", "FROM", "OF", "BY", "ON", "AT", "WITH", "FOR", "INTO", "UNDER", "OVER"}


def name_links(links: list[dict], labels: list[str], sheets: list[Sheet]) -> list[dict]:
    sheet_map = {s.name: s for s in sheets}
    answer = {}
    if links:

        def reason(link: dict) -> str:
            evidence = link.get("evidence", "")
            if isinstance(evidence, dict):
                evidence = evidence.get("reason", "")
            return str(link.get("why") or evidence or "relationship supported by the supplied data")

        text = "\n".join(f"{i}. {link['a']} and {link['b']}: {reason(link)}" for i, link in enumerate(links, 1))
        responses = []
        data_parts = _workbook_chunks(sheets) if get_settings().full_sheet_llm_analysis else [""]
        for data in data_parts:
            data_instruction = (
                "Complete workbook data. Analyze all supplied rows to verify direction and meaning:\n" + data
                if data
                else "Use the deterministic relationship evidence and node labels supplied above."
            )
            try:
                response = ask_json(
                    ENTITY_SYSTEM,
                    REL_PROMPT.format(labels=", ".join(labels), links=text, data_instruction=data_instruction),
                )
                responses.extend(response.get("relationships") or [])
            except Exception as exc:
                log.warning("relationship naming failed: %s", exc)
        grouped = defaultdict(list)
        for item in responses:
            if isinstance(item, dict) and str(item.get("id", "")).isdigit():
                grouped[int(item["id"])].append(item)
        for item_id, items in grouped.items():
            signatures = Counter(
                (str(item.get("type", "")), str(item.get("from", "")), str(item.get("to", ""))) for item in items
            )
            signature, _ = signatures.most_common(1)[0]
            answer[item_id] = next(item for item in items if (
                str(item.get("type", "")), str(item.get("from", "")), str(item.get("to", ""))
            ) == signature)
    rels = []
    for i, link in enumerate(links, 1):
        a = answer.get(i, {})
        ends = {link["a"], link["b"]}
        frm, to = gs.to_pascal(str(a.get("from", ""))), gs.to_pascal(str(a.get("to", "")))
        frm, to = gs.singular(frm), gs.singular(to)
        forward = (frm, to) == (link["a"], link["b"]) or link["a"] == link["b"] or {frm, to} != ends
        rtype = _short_rel(a.get("type")) or link.get("suggested_type") or f"HAS_{gs.to_rel_type(link['b'])}"
        if link["a"] == link["b"] and rtype.startswith("HAS_"):
            rtype = f"RELATED_{rtype[4:]}"
        src = (link["a"], link["a_column"]) if forward else (link["b"], link["b_column"])
        dst = (link["b"], link["b_column"]) if forward else (link["a"], link["a_column"])
        s = sheet_map[link["sheet"]]
        rels.append(
            {
                "type": rtype,
                "sheet": link["sheet"],
                "from": {"label": src[0], "column": src[1]},
                "to": {"label": dst[0], "column": dst[1]},
                "properties": [
                    {"name": gs.to_snake(c), "column": c, "type": s.profile[c].type} for c in link["properties"]
                ],
                "required": link["required"],
                "count": sum(
                    1 for r in s.rows if not is_blank(r.get(link["a_column"])) and not is_blank(r.get(link["b_column"]))
                ),
                "evidence": link.get("evidence", {}),
            }
        )
    # keep relationship types unique per (type, from, to)
    seen = set()
    for r in rels:
        base, n = r["type"], 2
        while (r["type"], r["from"]["label"], r["to"]["label"]) in seen:
            r["type"] = f"{base}_{n}"
            n += 1
        seen.add((r["type"], r["from"]["label"], r["to"]["label"]))
    return rels


# ------------------------------------------------------------------ step 5: PII
rule_pii = pii_detection.rule_pii
verify_pii = pii_detection.verify_pii


def detect_pii(sheet: Sheet, columns: list[str]) -> list[dict]:
    """Keep the extraction API stable while delegating PII work to its domain module."""
    return pii_detection.detect_pii(
        sheet,
        columns,
        ask_json=ask_json,
        analysis_data=_analysis_data,
        fix_column=_fix_column,
        rule_pii_fn=rule_pii,
        verify_pii_fn=verify_pii,
    )


# ------------------------------------------------------------------ orchestration
STEPS = [
    "Read file and detect sheets",
    "LLM identifies nodes and entities",
    "LLM infers relationships",
    "Generate Cypher queries",
    "LLM scans for PII",
]


def extract(sheets: list[Sheet], file_name: str, step=None, cancelled=None) -> dict:
    """Run steps 2-5 (step 1, reading, is done by the caller). step(index, status, detail)."""
    step = step or (lambda *a: None)
    check = cancelled or (lambda: False)
    names = [s.name for s in sheets]

    step(1, "running", f"0 of {len(sheets)} sheets")
    answers = {}
    hints = reference_hints(sheets)
    for i, s in enumerate(sheets, 1):
        answers[s.name] = ask_sheet_entities(s, file_name, names, hints[s.name])
        step(1, "running", f"{i} of {len(sheets)} sheets")
        if check():
            return {}
    nodes, per_sheet = build_nodes(
        sheets, answers, {k: {h.split('"')[1] for h in v if " holds IDs from sheet " in h} for k, v in hints.items()}
    )
    step(1, "done", f"{len(nodes)} node types")

    step(2, "running", "")
    links = detect_links(sheets, nodes, per_sheet)
    links = _merge_links(links + _discover_llm_links(sheets, nodes, links))
    rels = name_links(links, [n["label"] for n in nodes], sheets)
    step(2, "done", f"{len({r['type'] for r in rels})} relationship types")
    if check():
        return {}

    step(3, "running", "")
    schema = {"source_file": file_name, "sheets": sheets_summary(sheets), "nodes": nodes, "relationships": rels}
    schema = gs.normalize(schema)
    errors = gs.validate(schema)
    if errors:  # shouldn't happen: drop the offending relationships rather than fail the draft
        log.warning("draft schema problems: %s", errors)
        labels = {n["label"] for n in schema["nodes"]}
        schema["relationships"] = [
            r for r in schema["relationships"] if r["from"]["label"] in labels and r["to"]["label"] in labels
        ]
    step(3, "done", f"{len(gs.preview(schema))} statements")

    step(4, "running", "")
    pii = []
    for s in sheets:
        stored = stored_columns(schema, s.name)
        for item in detect_pii(s, stored):
            pii.append({"sheet": s.name, **item})
        if check():
            return {}
    schema["pii"] = nist_pii.assess(pii, schema["sheets"])
    step(4, "done", f"{len(pii)} PII columns")
    return schema


def sheets_summary(sheets: list[Sheet]) -> dict:
    return {
        s.name: {
            "header_row": s.header_row,
            "rows": len(s.rows),
            "columns": {
                c: {"type": p.type, "fill_rate": p.fill_rate, "distinct": p.distinct, "samples": p.samples[:3]}
                for c, p in s.profile.items()
            },
        }
        for s in sheets
    }


def stored_columns(schema: dict, sheet: str) -> list[str]:
    cols = []
    for n in schema["nodes"]:
        if n["sheet"] == sheet:
            cols += [n["key"]["column"]] + [p["column"] for p in n.get("properties", [])]
    for r in schema["relationships"]:
        if r["sheet"] == sheet:
            cols += [p["column"] for p in r["properties"]]
    return list(dict.fromkeys(cols))
