"""Load spreadsheet rows into Neo4j using an approved graph schema.

Used for the initial build (after Review) and for the add-data pipeline. Rules:
  * a row is rejected if a node key it defines is missing, a required relationship column is
    empty, or a relationship endpoint doesn't exist (in the graph or in accepted rows);
    rejection is all-or-nothing per row and reported
  * rejections cascade: a rejected row's key isn't available to other rows (fixpoint)
  * nodes merge on their key property (compared strip+upper, stored in the most common
    spelling or the spelling already in the graph); for repeated rows the last one wins
  * all nodes are written before relationships, so forward references are fine
  * a relationship marked "on_unknown": "skip" (graphs built from connectors) never rejects a row: when the
    record it points to doesn't exist, only that link is left out and counted in plan.links_skipped
"""

from collections import Counter, defaultdict
from dataclasses import dataclass, field

from app import graph_schema as gs
from app.graphstore import GraphStore
from app.tabular import Sheet, coerce, is_blank, match_columns, norm_key

MAX_REPORTED_REJECTIONS = 5000


class LoadError(ValueError):
    pass


@dataclass
class SheetMatch:
    schema_sheet: str
    file_sheet: Sheet
    columns: dict[str, str]  # schema column -> file column


@dataclass
class LoadPlan:
    matches: list[SheetMatch]
    unmatched_sheets: list[str]
    node_rows: dict[str, dict[str, dict]] = field(default_factory=dict)  # label -> key -> props
    rel_rows: dict[str, dict[tuple, dict]] = field(default_factory=dict)  # rel id -> (from, to) -> props
    rows_total: int = 0
    rows_loaded: int = 0
    rejected: list[dict] = field(default_factory=list)
    rejected_by_reason: Counter = field(default_factory=Counter)
    present_props: dict[str, set] = field(default_factory=dict)  # node label / rel id -> props in file
    links_skipped: Counter = field(default_factory=Counter)  # rel type -> links left out (unknown endpoint)


def sheet_required_columns(schema: dict, sheet: str) -> set[str]:
    cols = {n["key"]["column"] for n in schema["nodes"] if n["sheet"] == sheet and n.get("role", "row") == "row"}
    for r in schema["relationships"]:
        if r["sheet"] == sheet:
            cols |= {r["from"]["column"], r["to"]["column"]}
    return cols


def sheet_all_columns(schema: dict, sheet: str) -> set[str]:
    cols = sheet_required_columns(schema, sheet)
    for n in schema["nodes"]:
        if n["sheet"] == sheet:
            cols |= {n["key"]["column"]} | {p["column"] for p in n.get("properties", [])}
    for r in schema["relationships"]:
        if r["sheet"] == sheet:
            cols |= {p["column"] for p in r["properties"]}
    return cols


def match_sheets(schema: dict, sheets: list[Sheet]) -> tuple[list[SheetMatch], list[str]]:
    """Pair file sheets with schema sheets by their columns (names don't have to match)."""
    schema_sheets = sorted({n["sheet"] for n in schema["nodes"]} | {r["sheet"] for r in schema["relationships"]})
    matches, used, unmatched = [], set(), []
    for fs in sheets:
        best = None
        for ss in schema_sheets:
            if ss in used:
                continue
            required = sheet_required_columns(schema, ss)
            if not required:
                continue
            colmap = match_columns(sorted(sheet_all_columns(schema, ss)), fs.columns)
            if not required <= set(colmap):
                continue
            score = (len(colmap), ss.lower() == fs.name.lower())
            if best is None or score > best[0]:
                best = (score, ss, colmap)
        if best:
            used.add(best[1])
            matches.append(SheetMatch(best[1], fs, best[2]))
        else:
            unmatched.append(fs.name)
    return matches, unmatched


def _display_key(variants: Counter) -> str:
    """Most common spelling; ID-like keys (no spaces, contain a digit) are always upper-cased."""
    best = max(variants.items(), key=lambda kv: (kv[1], kv[0].isupper()))[0]
    if " " not in best and any(ch.isdigit() for ch in best):
        return best.upper()
    return best


def plan_load(
    schema: dict, sheets: list[Sheet], existing_keys: dict[str, dict[str, str]], merge_existing: bool = True
) -> LoadPlan:
    """Validate every row and build what will be written. existing_keys: label -> norm key -> stored key."""
    matches, unmatched = match_sheets(schema, sheets)
    if not matches:
        raise LoadError("None of the sheets in this file match the knowledge graph's schema.")
    plan = LoadPlan(matches, unmatched)
    labels = {n["label"]: n for n in schema["nodes"]}

    # rows per match with coerced values
    work = []  # (match, row, reasons)
    for m in matches:
        for row in m.file_sheet.rows:
            work.append((m, row))
    plan.rows_total = len(work)

    def cell(m: SheetMatch, row: dict, column: str):
        fc = m.columns.get(column)
        return row.get(fc) if fc else None

    rejected: dict[int, str] = {}
    available: dict[str, set] = {}
    for _ in range(10):  # fixpoint over cascading rejections
        available = {label: set(existing_keys.get(label, {})) for label in labels}
        for i, (m, row) in enumerate(work):
            if i in rejected:
                continue
            for n in schema["nodes"]:
                if n["sheet"] == m.schema_sheet:
                    k = norm_key(cell(m, row, n["key"]["column"]))
                    if k:
                        available[n["label"]].add(k)
        changed = False
        for i, (m, row) in enumerate(work):
            if i in rejected:
                continue
            reason = _row_problem(schema, m, row, available, existing_keys, merge_existing, cell)
            if reason:
                rejected[i] = reason
                changed = True
        if not changed:
            break

    for i, (m, row) in enumerate(work):
        if i in rejected:
            if len(plan.rejected) < MAX_REPORTED_REJECTIONS:
                plan.rejected.append({"sheet": m.file_sheet.name, "row": row["_row"], "reason": rejected[i]})
            plan.rejected_by_reason[rejected[i].split(":")[0]] += 1
    plan.rows_loaded = plan.rows_total - len(rejected)

    # build writes from accepted rows, in file order (last row wins)
    variants: dict[str, dict[str, Counter]] = defaultdict(lambda: defaultdict(Counter))
    for i, (m, row) in enumerate(work):
        if i in rejected:
            continue
        for n in schema["nodes"]:
            if n["sheet"] == m.schema_sheet:
                raw = cell(m, row, n["key"]["column"])
                if not is_blank(raw):
                    variants[n["label"]][norm_key(raw)][coerce(raw, "string")] += 1

    def stored(label: str, k: str) -> str:
        return existing_keys.get(label, {}).get(k) or _display_key(variants[label][k])

    for m in matches:
        for n in schema["nodes"]:
            if n["sheet"] == m.schema_sheet:
                plan.present_props.setdefault(n["label"], set()).update(
                    p["name"] for p in n.get("properties", []) if p["column"] in m.columns
                )
        for r in schema["relationships"]:
            if r["sheet"] == m.schema_sheet:
                plan.present_props.setdefault(r["id"], set()).update(
                    p["name"] for p in r["properties"] if p["column"] in m.columns
                )

    for i, (m, row) in enumerate(work):
        if i in rejected:
            continue
        for n in schema["nodes"]:
            if n["sheet"] != m.schema_sheet:
                continue
            k = norm_key(cell(m, row, n["key"]["column"]))
            if not k:
                continue
            props = {
                p["name"]: coerce(cell(m, row, p["column"]), p["type"])
                for p in n.get("properties", [])
                if p["column"] in m.columns
            }
            target = plan.node_rows.setdefault(n["label"], {})
            merged = target.get(k, {})
            # embedded entities repeat on many rows: don't let a blank cell wipe a known value
            merged.update({a: v for a, v in props.items() if v is not None or n.get("role") == "row"})
            target[k] = merged
        for r in schema["relationships"]:
            if r["sheet"] != m.schema_sheet:
                continue
            fk, tk = norm_key(cell(m, row, r["from"]["column"])), norm_key(cell(m, row, r["to"]["column"]))
            if not fk or not tk:
                continue
            if fk not in available[r["from"]["label"]] or tk not in available[r["to"]["label"]]:
                plan.links_skipped[r["type"]] += 1  # only "on_unknown": "skip" links get here
                continue
            props = {
                p["name"]: coerce(cell(m, row, p["column"]), p["type"])
                for p in r["properties"]
                if p["column"] in m.columns
            }
            plan.rel_rows.setdefault(r["id"], {})[(stored(r["from"]["label"], fk), stored(r["to"]["label"], tk))] = (
                props
            )

    # attach stored key spellings to node rows
    for label, rows in plan.node_rows.items():
        plan.node_rows[label] = {stored(label, k): v for k, v in rows.items()}
    return plan


def _row_problem(schema, m, row, available, existing_keys, merge_existing, cell) -> str | None:
    for n in schema["nodes"]:
        if n["sheet"] != m.schema_sheet or n.get("role", "row") != "row":
            continue
        k = norm_key(cell(m, row, n["key"]["column"]))
        if not k:
            return f"missing {n['key']['column']}: row has no {n['label']} key"
        if not merge_existing and k in existing_keys.get(n["label"], {}):
            return f"already exists: {n['label']} {k} is already in the graph"
    for r in schema["relationships"]:
        if r["sheet"] != m.schema_sheet:
            continue
        for end in ("from", "to"):
            raw = cell(m, row, r[end]["column"])
            if is_blank(raw):
                if r.get("required", True):
                    return f"missing {r[end]['column']}: needed for {r['type']}"
                break  # optional relationship: skip it for this row
            k = norm_key(raw)
            if k not in available[r[end]["label"]]:
                if r.get("on_unknown") == "skip":
                    break  # leave this link out, keep the row (counted in plan.links_skipped)
                return f"unknown {r[end]['label']}: '{str(raw).strip()}' in column {r[end]['column']}"
    return None


def existing_keys_for(store: GraphStore, schema: dict) -> dict[str, dict[str, str]]:
    out = {}
    for n in schema["nodes"]:
        key = n["key"]["name"]
        rows = store.read_internal(f"MATCH (n:{store.label(n['label'])}) RETURN n.`{key}` AS k")
        out[n["label"]] = {norm_key(r["k"]): r["k"] for r in rows if r["k"] is not None}
    return out


def execute_plan(store: GraphStore, schema: dict, plan: LoadPlan, progress=None, cancelled=None) -> dict:
    """Write nodes then relationships. progress(fraction, message); cancelled() -> bool."""
    store.ensure_storage()
    for n in schema["nodes"]:
        store.write(gs.index_cypher(n))
    totals = Counter()
    node_work = [(n, plan.node_rows.get(n["label"], {})) for n in schema["nodes"]]
    rel_work = [(r, plan.rel_rows.get(r["id"], {})) for r in schema["relationships"]]
    total = sum(len(x) for _, x in node_work) + sum(len(x) for _, x in rel_work) or 1
    done = 0

    def tick(message):
        if progress:
            progress(done / total, message)
        if cancelled and cancelled():
            raise LoadError("Cancelled")

    for n, rows in node_work:
        if not rows:
            continue
        key = n["key"]["name"]
        payload = [{key: k, **v} for k, v in rows.items()]
        props = sorted(plan.present_props.get(n["label"], set()))
        counts = store.write_batches(gs.node_cypher(n, store.kb_label, props), payload)
        totals.update(counts)
        done += len(rows)
        tick(f"Wrote {len(rows)} {n['label']} rows")
    for r, rows in rel_work:
        if not rows:
            continue
        payload = [{"from_key": a, "to_key": b, **v} for (a, b), v in rows.items()]
        props = sorted(plan.present_props.get(r["id"], set()))
        counts = store.write_batches(gs.relationship_cypher(r, schema, store.kb_label, props), payload)
        totals.update(counts)
        done += len(rows)
        tick(f"Wrote {len(rows)} {r['type']} relationships")
    return dict(totals)
