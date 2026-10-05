"""Neo4j access for one knowledge base, in either mode:

NEO4J_MODE=single  (Community): one shared database. Every node of a KB also carries the
                   label KB_<kb_name>; writes add it and chat queries are rewritten so every
                   node pattern is restricted to it.
NEO4J_MODE=multi   (Enterprise): one database per KB, created on demand. Sessions are bound to
                   that database, so queries can't see other KBs.

Chat Cypher is additionally checked to be read-only and executed in a read transaction.
"""

import re
import threading

from neo4j import READ_ACCESS, Driver, GraphDatabase, unit_of_work

from app.config import get_settings

_driver: Driver | None = None
_lock = threading.Lock()


def get_driver() -> Driver:
    global _driver
    with _lock:
        if _driver is None:
            s = get_settings()
            _driver = GraphDatabase.driver(
                s.neo4j_uri, auth=(s.neo4j_user, s.neo4j_password), max_connection_pool_size=20
            )
    return _driver


def close_driver() -> None:
    global _driver
    with _lock:
        if _driver is not None:
            _driver.close()
            _driver = None


class UnsafeQueryError(ValueError):
    pass


def database_name(kb_name: str) -> str:
    """Neo4j database names allow letters, digits, dots and dashes (no underscores)."""
    return kb_name.replace("_", "-").lower()


class GraphStore:
    def __init__(self, kb_name: str, mode: str | None = None, driver: Driver | None = None):
        self.kb_name = kb_name
        self.mode = mode or get_settings().neo4j_mode
        if self.mode not in ("single", "multi"):
            raise ValueError(f"NEO4J_MODE must be single or multi, not {self.mode!r}")
        self._driver = driver
        self.database = database_name(kb_name) if self.mode == "multi" else None
        self.kb_label = f"KB_{kb_name}" if self.mode == "single" else None

    @property
    def driver(self) -> Driver:
        return self._driver or get_driver()

    @property
    def storage_ref(self) -> str:
        return f"database:{self.database}" if self.mode == "multi" else f"label:{self.kb_label}"

    # -------------------------------------------------------------- lifecycle
    def ensure_storage(self) -> None:
        if self.mode == "multi":
            self.driver.execute_query(f"CREATE DATABASE `{self.database}` IF NOT EXISTS WAIT", database_="system")

    def drop(self) -> None:
        if self.mode == "multi":
            self.driver.execute_query(f"DROP DATABASE `{self.database}` IF EXISTS", database_="system")
        else:  # CALL ... IN TRANSACTIONS needs an auto-commit transaction
            with self.driver.session() as session:
                session.run(
                    f"MATCH (n:`{self.kb_label}`) CALL (n) {{ DETACH DELETE n }} " f"IN TRANSACTIONS OF 5000 ROWS"
                ).consume()

    # -------------------------------------------------------------- writes (loader only)
    def write(self, query: str, **params) -> dict:
        summary = self.driver.execute_query(query, params, database_=self.database).summary
        c = summary.counters
        return {
            "nodes_created": c.nodes_created,
            "relationships_created": c.relationships_created,
            "properties_set": c.properties_set,
        }

    def write_batches(self, query: str, rows: list[dict], batch_size: int = 1000, on_batch=None) -> dict:
        total = {"nodes_created": 0, "relationships_created": 0, "properties_set": 0}
        for i in range(0, len(rows), batch_size):
            for k, v in self.write(query, rows=rows[i : i + batch_size]).items():
                total[k] += v
            if on_batch:
                on_batch(min(i + batch_size, len(rows)), len(rows))
        return total

    def read_internal(self, query: str, **params) -> list[dict]:
        """Trusted, app-written read queries (labels already scoped by the caller)."""
        records = self.driver.execute_query(query, params, database_=self.database, routing_="r").records
        return [r.data() for r in records]

    def label(self, label: str) -> str:
        """Label expression for app-written queries."""
        return f"`{label}`:`{self.kb_label}`" if self.kb_label else f"`{label}`"

    # -------------------------------------------------------------- stats
    def counts(self) -> dict:
        if self.kb_label:
            nodes = self.read_internal(
                f"MATCH (n:`{self.kb_label}`) UNWIND [l IN labels(n) WHERE l <> $kb] AS label "
                f"RETURN label, count(*) AS n",
                kb=self.kb_label,
            )
            rels = self.read_internal(
                f"MATCH (:`{self.kb_label}`)-[r]->(:`{self.kb_label}`) RETURN type(r) AS type, count(*) AS n"
            )
        else:
            nodes = self.read_internal("MATCH (n) UNWIND labels(n) AS label RETURN label, count(*) AS n")
            rels = self.read_internal("MATCH ()-[r]->() RETURN type(r) AS type, count(*) AS n")
        return {"nodes": {r["label"]: r["n"] for r in nodes}, "relationships": {r["type"]: r["n"] for r in rels}}

    # -------------------------------------------------------------- chat queries (untrusted Cypher)
    def run_readonly(self, cypher: str, params: dict | None = None, limit: int = 200, timeout: float = 30.0):
        check_read_only(cypher)
        scoped = scope_cypher(cypher, self.kb_label) if self.kb_label else cypher

        def work(tx):
            result = tx.run(scoped, params or {})
            rows = []
            for rec in result:
                rows.append(rec.data())
                if len(rows) >= limit:
                    break
            return rows

        with self.driver.session(database=self.database, default_access_mode=READ_ACCESS) as session:
            return scoped, session.execute_read(unit_of_work(timeout=timeout)(work))


# ------------------------------------------------------------------ query safety
_STRING = re.compile(r"'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"")
_COMMENT = re.compile(r"//[^\n]*|/\*.*?\*/", re.S)
_FORBIDDEN = re.compile(
    r"\b(CREATE|MERGE|DELETE|DETACH|SET|REMOVE|DROP|LOAD\s+CSV|CALL|FOREACH|USE|ALTER|GRANT|REVOKE|DENY|"
    r"START|STOP|TERMINATE|SHOW|RENAME|ENABLE|DISABLE|FINISH|INSERT)\b",
    re.I,
)


def _mask_strings(cypher: str) -> tuple[str, list[str]]:
    literals = []

    def keep(m):
        literals.append(m.group(0))
        return f"__STR{len(literals) - 1}__"

    return _STRING.sub(keep, _COMMENT.sub(" ", cypher)), literals


def _unmask(text: str, literals: list[str]) -> str:
    return re.sub(r"__STR(\d+)__", lambda m: literals[int(m.group(1))], text)


def check_read_only(cypher: str) -> None:
    masked, _ = _mask_strings(cypher)
    if ";" in masked.strip().rstrip(";"):
        raise UnsafeQueryError("Only a single statement is allowed")
    m = _FORBIDDEN.search(masked)
    if m:
        raise UnsafeQueryError(f"Only read queries are allowed (found {m.group(1).upper()})")
    if re.search(r"\bKB_\w+", masked, re.I):
        raise UnsafeQueryError("Internal labels can't be referenced")


_NAME = r"(?:`[^`]+`|[A-Za-z_][A-Za-z0-9_]*)"
_NODE = re.compile(
    r"(?<![\w)\]`])\(\s*(?P<var>" + _NAME + r")?\s*(?P<labels>:\s*[^(){}]*?)?\s*"
    r"(?P<props>\{[^{}]*\})?\s*(?P<where>\bWHERE\b[^()]*)?\)"
)
_CLAUSE = re.compile(
    r"\b(OPTIONAL\s+MATCH|MATCH|WHERE|RETURN|WITH|UNWIND|ORDER\s+BY|SKIP|LIMIT|UNION|EXISTS|COUNT|" r"COLLECT)\b", re.I
)


def _in_pattern_context(text: str, start: int, end: int) -> bool:
    before = text[:start].rstrip()
    after = text[end:].lstrip()
    if before.endswith(("-", ">", "<")) or after.startswith(("-", "<")):
        return True
    if before.endswith(("(", "[")):  # shortestPath((a)...), [(a)-->(b) | ...]
        return after.startswith(("-", "<"))
    clauses = list(_CLAUSE.finditer(text[:start]))
    last = re.sub(r"\s+", " ", clauses[-1].group(1).upper()) if clauses else ""
    if before.upper().endswith("MATCH") or (before.endswith((",", "=")) and last in ("MATCH", "OPTIONAL MATCH")):
        return True
    return before.endswith("{") and last in ("MATCH", "OPTIONAL MATCH", "EXISTS", "COUNT", "COLLECT")


def scope_cypher(cypher: str, kb_label: str) -> str:
    """Add the KB label to every node pattern (single-database mode)."""
    masked, literals = _mask_strings(cypher)
    out, pos = [], 0
    for m in _NODE.finditer(masked):
        if not _in_pattern_context(masked, m.start(), m.end()):
            continue
        var, labels, props, where = (
            m.group("var") or "",
            (m.group("labels") or "").strip(),
            m.group("props"),
            m.group("where"),
        )
        expr = labels[1:].strip() if labels else ""
        if expr and re.search(r"[|&!%()]", expr):
            new_labels = f":({expr})&`{kb_label}`"
        elif expr:
            new_labels = f":{expr}:`{kb_label}`"
        else:
            new_labels = f":`{kb_label}`"
        node = f"({var}{new_labels}" + (f" {props}" if props else "") + (f" {where.strip()}" if where else "") + ")"
        out.append(masked[pos : m.start()] + node)
        pos = m.end()
    out.append(masked[pos:])
    scoped = "".join(out)
    _assert_fully_scoped(scoped, kb_label)
    return _unmask(scoped, literals)


_NODE_LIKE = re.compile(r"^\s*(?:" + _NAME + r")?\s*(?::|\{|\bWHERE\b|$)", re.I)


def _paren_groups(text: str) -> list[tuple[int, int]]:
    """(start, end) of every balanced (...) group, innermost first."""
    stack, out = [], []
    for i, ch in enumerate(text):
        if ch == "(":
            stack.append(i)
        elif ch == ")" and stack:
            out.append((stack.pop(), i + 1))
    return out


def _assert_fully_scoped(masked: str, kb_label: str) -> None:
    """Belt and braces: refuse the query if any node pattern ended up without the KB label.

    Checked on balanced parentheses, so node patterns the rewriting regex can't parse (an inline
    WHERE with function calls, e.g. `(n WHERE size(keys(n)) > 0)`) are refused instead of leaking
    other knowledge bases' nodes."""
    label = f"`{kb_label}`"
    for start, end in _paren_groups(masked):
        if start > 0 and re.match(r"[\w)\]`]", masked[start - 1]):
            continue  # a function call such as count(n) or shortestPath(...)
        inner = masked[start + 1 : end - 1]
        if not _NODE_LIKE.match(inner):
            continue  # an expression such as (o.total * 2) or a path group ((a)-->(b))
        if _in_pattern_context(masked, start, end) and label not in inner:
            raise UnsafeQueryError("Query could not be restricted to this knowledge base")
