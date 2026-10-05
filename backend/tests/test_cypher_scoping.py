"""Single-database mode: a KB's queries must never reach another KB's nodes.

Regression tests for node patterns the rewriting regex cannot parse (an inline WHERE with function calls):
they used to pass through unscoped and read every knowledge base in the shared database."""

import pytest

from app.graphstore import UnsafeQueryError, scope_cypher

LEAKS = [
    "MATCH (n WHERE size(keys(n)) > 0) RETURN labels(n), properties(n) LIMIT 50",
    "MATCH (n WHERE toLower(n.name) <> '') RETURN labels(n), n.name",
    "MATCH (a:Supplier)-->(n WHERE n.v > coalesce(n.w, 1)) RETURN n",
    "MATCH (a:Supplier), (n WHERE size(keys(n)) > 0) RETURN n",
    "MATCH (n:Supplier WHERE toLower(n.name) = 'x') RETURN n",
    "MATCH p = (n WHERE id(n) > 0)-->() RETURN p",
]


@pytest.mark.parametrize("query", LEAKS)
def test_unscopable_node_patterns_are_refused(query):
    with pytest.raises(UnsafeQueryError):
        scope_cypher(query, "KB_x")


@pytest.mark.parametrize(
    "query,expected",
    [
        (
            "MATCH (o:Order) RETURN o.total - (o.discount * o.total) AS net",
            "MATCH (o:Order:`KB_x`) RETURN o.total - (o.discount * o.total) AS net",
        ),
        (
            "MATCH (s:Supplier)-[:SUPPLIES]->(p:Product) WHERE (p.qty > 3) RETURN s.name",
            "MATCH (s:Supplier:`KB_x`)-[:SUPPLIES]->(p:Product:`KB_x`) WHERE (p.qty > 3) RETURN s.name",
        ),
        ("MATCH (a:A) WHERE (a)-->(:B) RETURN a", "MATCH (a:A:`KB_x`) WHERE (a:`KB_x`)-->(:B:`KB_x`) RETURN a"),
        ("MATCH (n WHERE n.v > 1) RETURN n", "MATCH (n:`KB_x` WHERE n.v > 1) RETURN n"),
        ("MATCH ((a:A)-->(b:B)){1,3} RETURN count(*)", "MATCH ((a:A:`KB_x`)-->(b:B:`KB_x`)){1,3} RETURN count(*)"),
    ],
)
def test_ordinary_queries_still_work(query, expected):
    assert scope_cypher(query, "KB_x") == expected
