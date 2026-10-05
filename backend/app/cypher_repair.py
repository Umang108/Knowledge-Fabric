"""Repair LLM-written Cypher against the knowledge base's schema before it runs.

Small models get the shape of a query right but the details wrong. Fixed here, deterministically:
  * label / relationship-type spelling      (PaidTo -> PAID_TO, supplier -> Supplier)
  * relationship direction                   ((p:Product)<-[:STORED_IN]-(w:Warehouse) -> ...-[:STORED_IN]->...)
  * properties on the wrong element          (w.stock_qty where stock_qty is on the relationship)
  * inline maps on the wrong element         ([r:CHARGED_TO {code: 'CC-IT'}]->(c:CostCentre) -> (c {code: ...}))
Anything that can't be repaired unambiguously is left alone; the query then fails or returns nothing
and the chat flow retries with the error.
"""

import re

from app.graphstore import _mask_strings, _unmask

_NAME = r"`?[A-Za-z_][A-Za-z0-9_]*`?"
_NODE = rf"\(\s*(?P<v>{_NAME})?\s*(?::\s*(?P<l>{_NAME}))?\s*(?P<m>\{{[^{{}}]*\}})?\s*\)"
_REL = (
    rf"(?P<lt><)?-\s*\[\s*(?P<rv>{_NAME})?\s*(?::\s*(?P<rt>{_NAME}))?\s*"
    rf"(?P<star>\*[^\]\{{]*)?\s*(?P<rm>\{{[^{{}}]*\}})?\s*\]\s*-(?P<gt>>)?"
)
NODE_RE = re.compile(_NODE)
REL_RE = re.compile(_REL)
_plain = lambda p: re.sub(r"\?P<\w+>", "?:", p)  # noqa: E731
CHAIN_RE = re.compile(rf"{_plain(_NODE)}(?:\s*{_plain(_REL)}\s*{_plain(_NODE)})+")


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.strip("`").lower())


class SchemaIndex:
    def __init__(self, schema: dict):
        self.labels = {_norm(n["label"]): n["label"] for n in schema["nodes"]}
        self.types = {_norm(r["type"]): r["type"] for r in schema["relationships"]}
        self.node_props = {
            n["label"]: {n["key"]["name"]} | {p["name"] for p in n.get("properties", [])} for n in schema["nodes"]
        }
        self.rel_props = {r["type"]: {p["name"] for p in r["properties"]} for r in schema["relationships"]}
        self.edges = {(r["from"]["label"], r["type"], r["to"]["label"]) for r in schema["relationships"]}

    def label(self, raw):
        return self.labels.get(_norm(raw)) if raw else None

    def rtype(self, raw):
        return self.types.get(_norm(raw)) if raw else None

    def direction(self, left, rtype, right):
        """'->', '<-' or None (unknown/ambiguous) for left-[rtype]-right."""
        if not rtype:
            return None
        fwd = any(
            e[1] == rtype and (left is None or e[0] == left) and (right is None or e[2] == right) for e in self.edges
        )
        back = any(
            e[1] == rtype and (right is None or e[0] == right) and (left is None or e[2] == left) for e in self.edges
        )
        if fwd and not back:
            return "->"
        if back and not fwd:
            return "<-"
        return None


def _map_keys(m: str | None) -> list[str]:
    return re.findall(r"([A-Za-z_]\w*)\s*:", m or "")


def _merge_map(a: str | None, extra: str) -> str:
    if not a:
        return "{" + extra + "}"
    inner = a.strip()[1:-1].strip()
    return "{" + (inner + ", " if inner else "") + extra + "}"


def _split_map(m: str) -> dict[str, str]:
    """{a: 1, b: 'x'} -> {'a': 'a: 1', ...} (strings are masked, so commas split safely)."""
    out = {}
    for part in m.strip()[1:-1].split(","):
        if ":" in part:
            out[part.split(":", 1)[0].strip()] = part.strip()
    return out


def repair(cypher: str, schema: dict) -> str:
    idx = SchemaIndex(schema)
    masked, literals = _mask_strings(cypher)

    # pass 1: variable -> label from every labelled node pattern
    var_label = {}
    for m in NODE_RE.finditer(masked):
        lab = idx.label(m.group("l"))
        if m.group("v") and lab:
            var_label[m.group("v").strip("`")] = lab
    rel_var_type = {}
    for m in REL_RE.finditer(masked):
        t = idx.rtype(m.group("rt"))
        if m.group("rv") and t:
            rel_var_type[m.group("rv").strip("`")] = t

    counter = [0]
    moves = {}  # (var, prop) -> replacement var

    def fix_chain(chain: str) -> str:
        nodes = list(NODE_RE.finditer(chain))
        rels = list(REL_RE.finditer(chain))
        # node records
        recs = []
        for n in nodes:
            v = (n.group("v") or "").strip("`")
            lab = idx.label(n.group("l")) or var_label.get(v)
            recs.append({"v": v, "l": n.group("l"), "lab": lab, "m": n.group("m")})
        rrecs = []
        for r in rels:
            t = idx.rtype(r.group("rt")) or rel_var_type.get((r.group("rv") or "").strip("`"))
            rrecs.append(
                {
                    "v": (r.group("rv") or "").strip("`"),
                    "t": t,
                    "raw_t": r.group("rt"),
                    "star": r.group("star"),
                    "m": r.group("rm"),
                    "dir": "<-" if r.group("lt") else "->" if r.group("gt") else "-",
                }
            )
        for i, rr in enumerate(rrecs):
            left, right = recs[i], recs[i + 1]
            if rr["dir"] != "-" and not rr["star"]:
                want = idx.direction(left["lab"], rr["t"], right["lab"])
                if want and want != rr["dir"]:
                    rr["dir"] = want
            # inline map keys on the relationship that belong to an endpoint node
            if rr["m"] and rr["t"]:
                keep, parts = [], _split_map(rr["m"])
                for key, text in parts.items():
                    if key in idx.rel_props.get(rr["t"], set()):
                        keep.append(text)
                        continue
                    target = next((n for n in (right, left) if n["lab"] and key in idx.node_props[n["lab"]]), None)
                    if target:
                        target["m"] = _merge_map(target["m"], text)
                    else:
                        keep.append(text)
                rr["m"] = "{" + ", ".join(keep) + "}" if keep else None
            # node property references that really live on this relationship
            for n in (left, right):
                if not n["v"] or not n["lab"] or not rr["t"]:
                    continue
                for prop in idx.rel_props.get(rr["t"], set()) - idx.node_props[n["lab"]]:
                    if re.search(rf"\b{re.escape(n['v'])}\.{prop}\b", masked):
                        if not rr["v"]:
                            counter[0] += 1
                            rr["v"] = f"_r{counter[0]}"
                        moves[(n["v"], prop)] = rr["v"]
            # relationship property references that really live on an endpoint
            if rr["v"] and rr["t"]:
                for n in (right, left):
                    if not n["lab"]:
                        continue
                    for prop in idx.node_props[n["lab"]] - idx.rel_props.get(rr["t"], set()):
                        if re.search(rf"\b{re.escape(rr['v'])}\.{prop}\b", masked) and n["v"]:
                            moves.setdefault((rr["v"], prop), n["v"])

        def node_text(n):
            lab = idx.label(n["l"]) or (n["l"].strip("`") if n["l"] else None)
            return "(" + n["v"] + (f":{lab}" if lab else "") + (f" {n['m']}" if n["m"] else "") + ")"

        def rel_text(r):
            t = r["t"] or (r["raw_t"].strip("`") if r["raw_t"] else None)
            body = r["v"] + (f":{t}" if t else "") + (r["star"] or "") + (f" {r['m']}" if r["m"] else "")
            return ("<-" if r["dir"] == "<-" else "-") + f"[{body}]" + ("->" if r["dir"] == "->" else "-")

        out = node_text(recs[0])
        for i, r in enumerate(rrecs):
            out += rel_text(r) + node_text(recs[i + 1])
        return out

    rebuilt, pos = [], 0
    for m in CHAIN_RE.finditer(masked):
        rebuilt.append(masked[pos : m.start()] + fix_chain(m.group(0)))
        pos = m.end()
    rebuilt.append(masked[pos:])
    text = "".join(rebuilt)
    # single node patterns outside chains: fix label spelling
    text = NODE_RE.sub(
        lambda m: "("
        + (m.group("v") or "")
        + (f":{idx.label(m.group('l')) or m.group('l')}" if m.group("l") else "")
        + (f" {m.group('m')}" if m.group("m") else "")
        + ")",
        text,
    )
    for (var, prop), new in moves.items():
        text = re.sub(rf"\b{re.escape(var)}\.{prop}\b", f"{new}.{prop}", text)
    return _unmask(text, literals)


def lint(cypher: str, schema: dict) -> list[str]:
    """Schema problems in a (repaired) query, phrased as feedback for the model's retry."""
    idx = SchemaIndex(schema)
    masked, _ = _mask_strings(cypher)
    problems = []
    var_label, rel_vars = {}, {}
    for m in NODE_RE.finditer(masked):
        raw = m.group("l")
        if raw and not idx.label(raw):
            problems.append(f"Unknown node label {raw.strip('`')}; valid labels: {', '.join(sorted(idx.node_props))}.")
        if m.group("v") and idx.label(raw):
            var_label[m.group("v").strip("`")] = idx.label(raw)
    edges_text = "; ".join(f"(:{a})-[:{t}]->(:{b})" for a, t, b in sorted(idx.edges))
    for chain in CHAIN_RE.finditer(masked):
        nodes = list(NODE_RE.finditer(chain.group(0)))
        for i, r in enumerate(REL_RE.finditer(chain.group(0))):
            raw_t = r.group("rt")
            t = idx.rtype(raw_t)
            if raw_t and not t:
                problems.append(f"Unknown relationship type {raw_t.strip('`')}; the relationships are: {edges_text}.")
                continue
            if r.group("rv") and t:
                rel_vars[r.group("rv").strip("`")] = t
            left = idx.label(nodes[i].group("l")) or var_label.get((nodes[i].group("v") or "").strip("`"))
            right = idx.label(nodes[i + 1].group("l")) or var_label.get((nodes[i + 1].group("v") or "").strip("`"))
            if (
                t
                and left
                and right
                and not r.group("star")
                and not ((left, t, right) in idx.edges or (right, t, left) in idx.edges)
            ):
                problems.append(f"{t} does not connect {left} and {right}. The relationships are: {edges_text}.")
    for var, prop in set(re.findall(r"\b([A-Za-z_]\w*)\.([A-Za-z_]\w*)\b", masked)):
        if var in var_label and prop not in idx.node_props[var_label[var]]:
            owners = [lab for lab, ps in idx.node_props.items() if prop in ps] + [
                t for t, ps in idx.rel_props.items() if prop in ps
            ]
            problems.append(
                f"{var_label[var]} has no property {prop}"
                + (f" ({prop} belongs to {', '.join(owners)})" if owners else "")
                + f"; {var_label[var]} properties: {', '.join(sorted(idx.node_props[var_label[var]]))}."
            )
        elif var in rel_vars and prop not in idx.rel_props.get(rel_vars[var], set()):
            owners = [lab for lab, ps in idx.node_props.items() if prop in ps]
            problems.append(
                f"Relationship {rel_vars[var]} has no property {prop}"
                + (f" ({prop} belongs to {', '.join(owners)})" if owners else "")
                + "."
            )
    return list(dict.fromkeys(problems))


ERROR_HINTS = [  # Neo4j error text -> advice a small model can act on
    (
        re.compile(r"aggregat", re.I),
        "Aggregations (max, min, sum, count) can't be used in WHERE or inside node "
        "patterns. For the largest/smallest value use ORDER BY x.prop DESC LIMIT 1; "
        "for totals use WITH n, sum(...) AS total.",
    ),
    (re.compile(r"not defined", re.I), "Every variable used in RETURN/WHERE must be introduced in a MATCH first."),
    (re.compile(r"Invalid input", re.I), "Check brackets and quotes; write one MATCH ... RETURN statement."),
]


def error_hint(message: str) -> str:
    return " ".join(h for rx, h in ERROR_HINTS if rx.search(message or ""))
