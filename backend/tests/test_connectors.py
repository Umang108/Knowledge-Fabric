"""SAP and ServiceNow connectors, against stand-in servers with the real APIs' response shapes.

Covers the connectors themselves (paging, auth, retries, value cleaning, incremental pulls), the saved-connection
API (secrets encrypted, owner-only), and the end-to-end flows: build a graph KB or a RAG KB from a connection,
then refresh it with "add data from connection". Neo4j writes go to an in-memory store; Chroma is real.
"""

import re
from collections import Counter

import pytest

from app import auth, extraction, jobs, kb, loader, pipelines, rag
from app.auth import CurrentUser
from app.cli import main as cli
from app.connectors import ConnectorError, Dataset, SapODataConnector, ServiceNowConnector
from app.connectors import pipeline as cp
from app.connectors.base import Table
from app.connectors.servicenow import PRESETS as SN_PRESETS
from app.connectors.servicenow import html_to_text
from app.db import get_conn
from app.tabular import read_table_file

from .conftest import login
from .keycloak_mock import ServerThread
from .source_mocks import (
    SAP_CLIENT,
    SAP_PASSWORD,
    SAP_USER,
    SINCE,
    SN_CLIENT,
    SN_CLIENT_SECRET,
    SN_PASSWORD,
    SN_USER,
    MockSap,
    MockServiceNow,
)
from .test_mcp_server import HashEmbeddings

SN_ITSM = SN_PRESETS["itsm"]["datasets"]
SN_KNOWLEDGE = SN_PRESETS["knowledge"]["datasets"]


@pytest.fixture
def servicenow():
    mock = MockServiceNow()
    with ServerThread(mock.app) as srv:
        mock.url = srv.url
        yield mock


@pytest.fixture
def sap():
    mock = MockSap()
    with ServerThread(mock.app) as srv:
        mock.url = srv.url
        yield mock


def sn(mock, **kw) -> ServiceNowConnector:
    c = ServiceNowConnector(
        mock.url,
        kw.pop("auth_type", "basic"),
        kw.pop("username", SN_USER),
        kw.pop("secret", SN_PASSWORD),
        kw.pop("options", {}),
    )
    c.page_size = kw.pop("page_size", 50)
    return c


def sp(mock, **options) -> SapODataConnector:
    return SapODataConnector(
        mock.url, "basic", SAP_USER, options.pop("secret", SAP_PASSWORD), {"sap_client": SAP_CLIENT, **options}
    )


# ------------------------------------------------------------------ ServiceNow connector
def test_servicenow_table_paging_references_and_choices(servicenow):
    ds = Dataset.from_dict(SN_ITSM[0])  # incidents
    with sn(servicenow) as c:
        t = c.fetch_table(ds)
    assert len(t.rows) == 260 and len({r["sys_id"] for r in t.rows}) == 260
    assert sum(1 for r in servicenow.requests if r["table"] == "incident") == 6  # 50 per page
    assert t.columns[:4] == ["sys_id", "number", "short_description", "state"]
    assert t.columns[t.columns.index("caller_id") + 1] == "caller_id_name"  # id to link on, then the name
    users = {u["sys_id"]: u["name"] for u in servicenow.data["sys_user"]}
    row = t.rows[0]
    assert row["caller_id_name"] == users[row["caller_id"]]
    assert row["state"] in {"New", "In Progress", "Resolved"}  # choice label, not "2"
    assert re.match(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", row["opened_at"])  # stored value kept
    assert row["resolved_at"] is None  # empty -> blank
    q = next(r for r in servicenow.requests if r["table"] == "incident")
    assert q["sysparm_display_value"] == "all" and q["sysparm_query"] == "ORDERBYsys_created_on"
    assert q["sysparm_fields"].split(",")[0] == "sys_id"


def test_servicenow_oauth_retry_and_errors(servicenow):
    with sn(
        servicenow, auth_type="oauth", username=None, secret=SN_CLIENT_SECRET, options={"client_id": SN_CLIENT}
    ) as c:
        servicenow.throttle_next = 2  # two 429s, then the data
        assert len(c.fetch_table(Dataset.from_dict({"name": "Groups", "source": "sys_user_group"})).rows) == 5
    with pytest.raises(ConnectorError, match="refused the OAuth client"):
        sn(servicenow, auth_type="oauth", secret="wrong", options={"client_id": SN_CLIENT}).fetch_table(
            Dataset.from_dict({"source": "sys_user"})
        )
    with pytest.raises(ConnectorError, match="refused the credentials"):
        sn(servicenow, secret="wrong").fetch_table(Dataset.from_dict({"source": "sys_user"}))
    with pytest.raises(ConnectorError, match="HTTP 404"):
        sn(servicenow).fetch_table(Dataset.from_dict({"source": "no_such_table"}))
    with pytest.raises(ConnectorError, match="not a ServiceNow table"):
        sn(servicenow).fetch_table(Dataset.from_dict({"source": "incident; drop"}))


def test_servicenow_only_changes_since(servicenow):
    with sn(servicenow) as c:
        t = c.fetch_table(Dataset.from_dict(SN_ITSM[0]), since=SINCE)
    expected = [i for i in servicenow.data["incident"] if i["sys_updated_on"] >= f"{SINCE} 00:00:00"]
    assert len(t.rows) == len(expected) > 0
    assert "gs.dateGenerate('2026-10-01','00:00:00')" in servicenow.requests[-1]["sysparm_query"]


def test_servicenow_knowledge_articles_become_documents(servicenow):
    with sn(servicenow) as c:
        docs = dict(c.fetch_documents(Dataset.from_dict(SN_KNOWLEDGE[0])))
    assert len(docs) == 4  # the draft is not published
    vpn = next(text for name, text in docs.items() if "VPN" in name)
    assert vpn.startswith("# Reset your VPN token") and "- Choose VPN" in vpn and "<" not in vpn
    assert "alert(1)" not in "".join(docs.values())  # scripts are dropped
    assert "Standard | 4" in html_to_text("<table><tr><td>Standard</td><td>4</td></tr></table>")


# ------------------------------------------------------------------ SAP connector
def test_sap_v2_paging_cleaning_and_select(sap):
    with sp(sap) as c:
        t = c.fetch_table(Dataset.from_dict({"name": "Orders", "source": "API_SALES_ORDER_SRV/A_SalesOrder"}))
    assert len(t.rows) == 120 and len({r["SalesOrder"] for r in t.rows}) == 120
    assert sum(1 for r in sap.requests if r["path"].endswith("A_SalesOrder")) == 3  # server pages of 50
    assert "$skiptoken" in sap.requests[-1] and sap.requests[-1]["sap-client"] == SAP_CLIENT
    row = t.rows[0]
    assert "__metadata" not in row and "to_Item" not in row  # metadata and navigation links dropped
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", row["CreationDate"])  # /Date(ms)/ -> ISO date
    with sp(sap) as c:
        t = c.fetch_table(
            Dataset.from_dict(
                {
                    "source": "API_BUSINESS_PARTNER/A_BusinessPartner",
                    "fields": ["BusinessPartner", "BusinessPartnerFullName"],
                }
            )
        )
    assert t.columns == ["BusinessPartner", "BusinessPartnerFullName"]


def test_sap_v4_next_links_and_incremental(sap):
    with sp(sap, odata_version="4") as c:
        t = c.fetch_table(
            Dataset.from_dict({"name": "Products", "source": "api_product/srvd_a2x/sap/product/0001/Product"})
        )
    assert len(t.rows) == 40 and "@odata.etag" not in t.columns
    ds = Dataset.from_dict({"source": "API_SALES_ORDER_SRV/A_SalesOrder", "changed_field": "LastChangeDateTime"})
    with sp(sap) as c:
        changed = c.fetch_table(ds, since=SINCE)
    assert 0 < len(changed.rows) < 120
    assert "LastChangeDateTime ge datetimeoffset'2026-10-01T00:00:00Z'" in sap.requests[-1]["$filter"]
    with sp(sap) as c, pytest.raises(ConnectorError, match="changed field"):
        c.fetch_table(Dataset.from_dict({"source": "API_PRODUCT_SRV/A_Product"}), since=SINCE)


def test_sap_errors(sap):
    with pytest.raises(ConnectorError, match="refused the credentials"):
        sp(sap, secret="wrong").fetch_table(Dataset.from_dict({"source": "API_PRODUCT_SRV/A_Product"}))
    with pytest.raises(ConnectorError, match="refused the credentials"):  # wrong client is a logon failure
        sp(sap, sap_client="200").fetch_table(Dataset.from_dict({"source": "API_PRODUCT_SRV/A_Product"}))
    with pytest.raises(ConnectorError, match="not an OData path"):
        sp(sap).fetch_table(Dataset.from_dict({"source": "../../etc/passwd"}))


def test_urls_and_allowed_hosts(settings):
    with pytest.raises(ConnectorError, match="https://"):
        ServiceNowConnector("file:///etc/passwd", "basic", "u", "p", {})
    settings(CONNECTOR_ALLOWED_HOSTS="*.service-now.com")
    ServiceNowConnector("https://acme.service-now.com", "basic", "u", "p", {}).close()
    with pytest.raises(ConnectorError, match="CONNECTOR_ALLOWED_HOSTS"):
        ServiceNowConnector("https://intranet.example.com", "basic", "u", "p", {})


def test_numeric_document_numbers_named_after_the_entity_are_keys(tmp_path):
    """SAP names numeric keys after the entity (SalesOrder 1000001), not "..._id": the LLM's choice must survive
    validation, while an unnamed numeric measure still can't be a key."""
    rows = [{"SalesOrder": str(1000000 + i), "NetQty": str(i * 7 + 3), "Status": "A"} for i in range(150)]
    cp.write_workbook(tmp_path / "so.xlsx", [Table("SalesOrders", ["SalesOrder", "NetQty", "Status"], rows)])
    sheet = read_table_file(tmp_path / "so.xlsx")[0]
    assert sheet.profile["SalesOrder"].type == "integer"
    assert extraction._clean_entity({"label": "SalesOrder", "key_column": "SalesOrder"}, sheet)["key_column"]
    assert extraction._clean_entity({"label": "Order", "key_column": "NetQty"}, sheet) is None
    assert extraction._heuristic_key(sheet) == "SalesOrder"


def test_servicenow_workbook_extracts_a_linked_graph(servicenow, tmp_path, monkeypatch):
    """The workbook a connector writes goes through the normal extraction (here without an LLM)."""
    monkeypatch.setattr(extraction, "ask_json", lambda *a, **k: (_ for _ in ()).throw(ValueError("no LLM")))
    with sn(servicenow) as c:
        tables = [c.fetch_table(Dataset.from_dict(d)) for d in SN_ITSM]
    cp.write_workbook(tmp_path / "itsm.xlsx", tables)
    sheets = read_table_file(tmp_path / "itsm.xlsx")
    assert [s.name for s in sheets] == ["Incidents", "Problems", "Changes", "Users", "Groups", "ConfigurationItems"]
    schema = extraction.extract(sheets, "itsm.xlsx")
    links = {(r["sheet"], r["from"]["column"], r["to"]["column"]) for r in schema["relationships"]}
    by_sheet = Counter(s for s, _, _ in links)
    assert by_sheet["Incidents"] >= 4  # caller, assignee, group, CI (and problem) link by sys_id
    assert any({"caller_id", "sys_id"} == {f, t} or "caller_id" in (f, t) for s, f, t in links if s == "Incidents")
    strict = loader.plan_load(schema, sheets, {})
    plan = loader.plan_load(cp.relax_schema(schema), sheets, {})  # what a connector-built KB stores
    assert not plan.rejected and sum(len(v) for v in plan.node_rows.values())
    assert len(strict.rejected) > 0  # the strict upload contract would have dropped rows (blank manager etc.)
    assert {p["column"] for p in schema["pii"] if p["sheet"] == "Users"} >= {"email"}


# ------------------------------------------------------------------ API: saved connections
class MemoryGraphStore:
    """In-memory Neo4j stand-in for the loader (index, node and relationship writes; key lookups)."""

    nodes: dict = {}

    def __init__(self, kb_name, *a, **k):
        self.kb_name, self.kb_label, self.storage_ref = kb_name, f"KB_{kb_name}", f"label:KB_{kb_name}"
        MemoryGraphStore.nodes.setdefault(kb_name, {})

    def label(self, label):
        return f"`{label}`:`{self.kb_label}`"

    def ensure_storage(self):
        pass

    def write(self, query, **params):
        return {"nodes_created": 0, "relationships_created": 0, "properties_set": 0}

    def write_batches(self, query, rows, batch_size=1000, on_batch=None):
        m = re.search(r"MERGE \(n:`(\w+)`", query)
        if m:
            store = MemoryGraphStore.nodes[self.kb_name].setdefault(m.group(1), {})
            key = re.search(r"\{(\w+): row\.", query).group(1)
            new = sum(1 for r in rows if r[key] not in store)
            store.update({r[key]: r for r in rows})
            return {"nodes_created": new, "relationships_created": 0, "properties_set": 0}
        return {"nodes_created": 0, "relationships_created": len(rows), "properties_set": 0}

    def read_internal(self, query, **params):
        m = re.search(r"MATCH \(n:`(\w+)`", query)
        store = MemoryGraphStore.nodes[self.kb_name].get(m.group(1), {}) if m else {}
        return [{"k": k} for k in store]

    def counts(self):
        return {"nodes": {k: len(v) for k, v in MemoryGraphStore.nodes[self.kb_name].items()}, "relationships": {}}


@pytest.fixture
def app_env(client, settings, monkeypatch, tmp_path):
    settings(UPLOAD_DIR=str(tmp_path / "uploads"))
    cli(["seed-demo-users"])
    monkeypatch.setattr(extraction, "ask_json", lambda *a, **k: (_ for _ in ()).throw(ValueError("no LLM")))
    monkeypatch.setattr(rag, "get_embeddings", lambda: HashEmbeddings())
    monkeypatch.setattr(pipelines, "GraphStore", MemoryGraphStore)
    MemoryGraphStore.nodes = {}
    return {"priya": login(client, "priya.nair"), "arjun": login(client, "arjun.mehta")}


def _connect(client, headers, mock, kind="servicenow", **over):
    body = {
        "name": f"{kind} test",
        "kind": kind,
        "base_url": mock.url,
        "auth_type": "basic",
        "username": SN_USER if kind == "servicenow" else SAP_USER,
        "secret": SN_PASSWORD if kind == "servicenow" else SAP_PASSWORD,
        "options": {"sap_client": SAP_CLIENT} if kind == "sap" else {},
    }
    r = client.post("/api/connections", headers=headers, json={**body, **over})
    assert r.status_code == 201, r.text
    return r.json()


def test_connections_are_private_and_secrets_encrypted(client, app_env, servicenow):
    priya, arjun = app_env["priya"], app_env["arjun"]
    c = _connect(client, priya, servicenow)
    assert "secret" not in c and c["has_secret"] is True
    with get_conn() as conn:
        stored = conn.execute("SELECT secret FROM connections WHERE id = %s", (c["id"],)).fetchone()["secret"]
    assert SN_PASSWORD not in stored and auth.decrypt(stored) == SN_PASSWORD
    assert client.post(f"/api/connections/{c['id']}/test", headers=arjun).status_code == 404  # not his
    assert client.get("/api/connections", headers=arjun).json() == []
    r = client.put(f"/api/connections/{c['id']}", headers=priya, json={"name": "SN prod"})  # secret kept
    assert r.status_code == 200 and r.json()["name"] == "SN prod" and r.json()["has_secret"]
    assert client.post(f"/api/connections/{c['id']}/test", headers=priya).json()["ok"] is True
    listed = client.get("/api/connections", headers=priya).json()[0]
    assert listed["last_test_ok"] is True and listed["last_test_detail"].startswith("Connected")
    bad = _connect(client, priya, servicenow, name="wrong pw", secret="nope")
    t = client.post(f"/api/connections/{bad['id']}/test", headers=priya).json()
    assert t["ok"] is False and "refused the credentials" in t["detail"]
    for body, msg in [
        ({"base_url": "ftp://x"}, "https://"),
        ({"kind": "oracle"}, "kind must be"),
        ({"auth_type": "oauth", "options": {}}, "client id"),
        ({"secret": ""}, "password"),
    ]:
        r = client.post(
            "/api/connections",
            headers=priya,
            json={
                "name": "x",
                "kind": "servicenow",
                "base_url": servicenow.url,
                "auth_type": "basic",
                "username": "u",
                "secret": "s",
                **body,
            },
        )
        assert r.status_code == 422 and msg in r.text, body
    assert client.delete(f"/api/connections/{bad['id']}", headers=priya).status_code == 200
    kinds = client.get("/api/connectors", headers=priya).json()["kinds"]
    assert {k["kind"] for k in kinds} == {"servicenow", "sap"} and "itsm" in kinds[0]["presets"]


def test_preview_shows_the_first_rows(client, app_env, servicenow):
    c = _connect(client, app_env["priya"], servicenow)
    r = client.post(
        f"/api/connections/{c['id']}/preview",
        headers=app_env["priya"],
        json={"dataset": {"name": "Users", "source": "sys_user", "fields": ["user_name", "email"]}},
    )
    assert (
        r.status_code == 200 and len(r.json()["rows"]) == 5 and r.json()["columns"] == ["sys_id", "user_name", "email"]
    )
    r = client.post(
        f"/api/connections/{c['id']}/preview", headers=app_env["priya"], json={"dataset": {"source": "Bad Table"}}
    )
    assert r.status_code == 422


def test_graph_kb_from_servicenow_then_refresh_with_changes(client, app_env, servicenow):
    priya = app_env["priya"]
    c = _connect(client, priya, servicenow)
    r = client.post(
        "/api/kbs/from-connection",
        headers=priya,
        json={
            "kb_name": "t_itsm_kg",
            "kb_type": "graph",
            "domain": "IT",
            "sub_domain": "Service management",
            "connection_id": c["id"],
            "datasets": SN_ITSM,
        },
    )
    assert r.status_code == 201, r.text
    job = jobs.wait(r.json()["job_id"], timeout=120)
    assert job["status"] == "succeeded", job["error"]
    assert job["steps"][0]["name"] == "Fetch from ServiceNow" and "Incidents: 260 rows" in job["steps"][0]["detail"]
    assert all(s["status"] == "done" for s in job["steps"])
    cat = kb.get_catalog("t_itsm_kg")
    assert cat["status"] == "awaiting_review" and cat["draft_schema"]["source_file"].startswith("servicenow_")

    # the owner reviews and submits; the graph is built (in-memory store) and becomes ready
    review = client.get("/api/kbs/t_itsm_kg/review", headers=priya).json()
    r = client.post("/api/kbs/t_itsm_kg/submit", headers=priya, json={"schema": review["schema"]})
    assert jobs.wait(r.json()["job_id"], timeout=120)["status"] == "succeeded"
    assert kb.get_catalog("t_itsm_kg")["status"] == "ready"
    incident_label = next(
        n["label"] for n in review["schema"]["nodes"] if n["sheet"] == "Incidents" and n["role"] == "row"
    )
    assert len(MemoryGraphStore.nodes["t_itsm_kg"][incident_label]) == 260

    # a user with access refreshes it from their own connection, changes since 1 Oct only
    assert client.post("/api/kbs/t_itsm_kg/access", headers=priya, json={"user_id": "arjun.mehta"}).status_code == 201
    mine = _connect(client, app_env["arjun"], servicenow, name="arjun sn")
    servicenow.data["incident"].append(
        {
            **servicenow.data["incident"][-1],
            "sys_id": "f" * 32,
            "number": "INC9999999",
            "sys_updated_on": "2026-10-04 08:00:00",
        }
    )
    r = client.post(
        "/api/kbs/t_itsm_kg/add-data/connection",
        headers=app_env["arjun"],
        json={"connection_id": mine["id"], "since": SINCE, "datasets": [SN_ITSM[0]]},
    )
    assert r.status_code == 202, r.text
    job = jobs.wait(r.json()["job_id"], timeout=120)
    assert job["status"] == "succeeded", job["error"]
    run = client.get("/api/kbs/t_itsm_kg/runs", headers=priya).json()[0]
    changed = [i for i in servicenow.data["incident"] if i["sys_updated_on"] >= f"{SINCE} 00:00:00"]
    assert run["run_type"] == "add_data" and run["rows_total"] == len(changed) and run["started_by"] == "arjun.mehta"
    assert len(MemoryGraphStore.nodes["t_itsm_kg"][incident_label]) == 261  # one new, the rest merged

    # someone without access can't, even with a working connection
    outsider = login(client, "karthik.r")
    theirs = _connect(client, outsider, servicenow, name="k sn")
    r = client.post(
        "/api/kbs/t_itsm_kg/add-data/connection",
        headers=outsider,
        json={"connection_id": theirs["id"], "datasets": [SN_ITSM[0]]},
    )
    assert r.status_code == 403


def test_rag_kb_from_servicenow_knowledge_is_searchable(client, app_env, servicenow):
    from app import mcp_server

    priya = app_env["priya"]
    c = _connect(client, priya, servicenow)
    rag.drop_collection("t_sn_kb_rag")
    try:
        r = client.post(
            "/api/kbs/from-connection",
            headers=priya,
            json={
                "kb_name": "t_sn_kb_rag",
                "kb_type": "rag",
                "domain": "IT",
                "sub_domain": "Knowledge",
                "connection_id": c["id"],
                "datasets": SN_KNOWLEDGE,
            },
        )
        job = jobs.wait(r.json()["job_id"], timeout=120)
        assert job["status"] == "succeeded", job["error"]
        assert kb.get_catalog("t_sn_kb_rag")["status"] == "ready"
        user = CurrentUser("priya.nair", "Priya Nair", None)
        hits = mcp_server.search_documents(user, "t_sn_kb_rag", "how do I reset my VPN token", 2)["passages"]
        assert "VPN" in hits[0]["source"] and "Reset token" in hits[0]["text"]
        assert len(rag.documents("t_sn_kb_rag")) == 4

        r = client.post(
            "/api/kbs/t_sn_kb_rag/add-data/connection",
            headers=priya,
            json={"connection_id": c["id"], "since": SINCE, "datasets": SN_KNOWLEDGE},
        )
        assert jobs.wait(r.json()["job_id"], timeout=120)["status"] == "succeeded"
        assert len(rag.documents("t_sn_kb_rag")) == 4  # the one changed article replaced, not duplicated
    finally:
        rag.drop_collection("t_sn_kb_rag")


def test_graph_kb_from_sap_links_orders_to_partners(client, app_env, sap):
    priya = app_env["priya"]
    c = _connect(client, priya, sap, kind="sap")
    from app.connectors.sap import PRESETS

    r = client.post(
        "/api/kbs/from-connection",
        headers=priya,
        json={
            "kb_name": "t_sap_sales_kg",
            "kb_type": "graph",
            "domain": "Sales",
            "sub_domain": "Orders",
            "connection_id": c["id"],
            "datasets": PRESETS["sales"]["datasets"],
        },
    )
    job = jobs.wait(r.json()["job_id"], timeout=120)
    assert job["status"] == "succeeded", job["error"] and job["steps"][0]["name"] == "Fetch from SAP"
    schema = kb.get_catalog("t_sap_sales_kg")["draft_schema"]
    links = {(r["sheet"], frozenset((r["from"]["column"], r["to"]["column"]))) for r in schema["relationships"]}
    assert ("SalesOrders", frozenset({"SalesOrder", "SoldToParty"})) in links
    assert any(s == "SalesOrderItems" and "Material" in cols for s, cols in links)

    bad = [
        ({"kb_type": "rag"}, "builds a knowledge graph"),
        ({"datasets": [{"source": "../secret"}]}, "not an OData path"),
        ({"datasets": []}, ""),
    ]
    for override, msg in bad:
        body = {
            "kb_name": "t_sap_bad_kg",
            "kb_type": "graph",
            "domain": "d",
            "sub_domain": "s",
            "connection_id": c["id"],
            "datasets": PRESETS["sales"]["datasets"],
            **override,
        }
        r = client.post("/api/kbs/from-connection", headers=priya, json=body)
        assert r.status_code == 422 and msg in r.text, override
    assert kb.get_catalog("t_sap_bad_kg") is None


def test_failed_fetch_marks_the_kb_failed_with_the_reason(client, app_env, servicenow):
    priya = app_env["priya"]
    c = _connect(client, priya, servicenow, secret="wrong")
    r = client.post(
        "/api/kbs/from-connection",
        headers=priya,
        json={
            "kb_name": "t_sn_fail_kg",
            "kb_type": "graph",
            "domain": "IT",
            "sub_domain": "x",
            "connection_id": c["id"],
            "datasets": SN_ITSM[:1],
        },
    )
    job = jobs.wait(r.json()["job_id"], timeout=60)
    assert job["status"] == "failed" and "refused the credentials" in job["error"]
    assert kb.get_catalog("t_sn_fail_kg")["status"] == "failed"


def test_record_guids_are_not_pii(tmp_path):
    """A 32-hex sys_id contains digit runs that look like phone numbers; it is an identifier, not free text."""
    import uuid as _uuid

    rows = [
        {"sys_id": _uuid.UUID(int=i * 7919 + 10**30).hex, "notes": f"Call me on 98450{i:05d} about it"}
        for i in range(60)
    ]
    cp.write_workbook(tmp_path / "g.xlsx", [Table("Incidents", ["sys_id", "notes"], rows)])
    sheet = read_table_file(tmp_path / "g.xlsx")[0]
    assert extraction.rule_pii(sheet, "sys_id") is None
    assert not extraction.verify_pii(sheet, "sys_id", "free_text")
    assert not extraction.verify_pii(sheet, "sys_id", "phone")
    assert extraction.rule_pii(sheet, "notes")["category"] == "free_text"  # real text still is
