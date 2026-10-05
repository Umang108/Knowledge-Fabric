"""Stand-ins for ServiceNow and SAP, served over real HTTP, with the response shapes of the real APIs.

ServiceNow Table API (GET /api/now/table/{table}):
  sysparm_display_value=all -> every field is {"display_value", "value"}; references carry the sys_id as value
  sysparm_fields / sysparm_limit / sysparm_offset / sysparm_query (field=value, sys_updated_on>=gs.dateGenerate)
  X-Total-Count header; Basic auth or OAuth client credentials (/oauth_token.do); optional 429 on demand.
SAP OData v2 (/sap/opu/odata/sap/{SERVICE}/{EntitySet}):
  {"d": {"results": [...], "__next": "...$skiptoken=N"}}, __metadata, deferred navigation properties,
  /Date(ms)/ values, $select/$top/$skip/$filter, sap-client, server page size 50.
SAP OData v4 (/sap/opu/odata4/sap/.../Product): {"value": [...], "@odata.nextLink": "Product?$skiptoken=N"}.
"""

import base64
import datetime as dt
import random
import re
import uuid

from fastapi import FastAPI, Form, Request
from fastapi.responses import JSONResponse

SN_USER, SN_PASSWORD = "integration.user", "sn-secret"
SN_CLIENT, SN_CLIENT_SECRET = "sn-client", "sn-client-secret"
SAP_USER, SAP_PASSWORD, SAP_CLIENT = "COMM_USER", "sap-secret", "100"
SINCE = "2026-10-01"


def _sys_id(kind: str, i: int) -> str:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"{kind}/{i}").hex


# ------------------------------------------------------------------ ServiceNow data
def servicenow_data(seed: int = 7) -> dict:
    rnd = random.Random(seed)
    first = ["Asha", "Ravi", "Meera", "John", "Li", "Sara", "Omar", "Priya", "Karl", "Nina"]
    last = ["Rao", "Iyer", "Smith", "Chen", "Khan", "Das", "Mehta", "Novak", "Ali", "Bose"]
    users = []
    for i in range(40):
        name = f"{first[i % 10]} {last[(i // 10 + i) % 10]}"
        users.append(
            {
                "sys_id": _sys_id("user", i),
                "user_name": name.lower().replace(" ", "."),
                "name": name,
                "email": f"{name.lower().replace(' ', '.')}@example.test",
                "title": rnd.choice(["Engineer", "Analyst", "Manager", "Technician"]),
                "department": rnd.choice(["IT", "Finance", "HR", "Operations"]),
                "manager": _sys_id("user", 0) if i else "",
            }
        )
    groups = [
        {
            "sys_id": _sys_id("group", i),
            "name": n,
            "manager": _sys_id("user", i + 1),
            "description": f"{n} support team",
        }
        for i, n in enumerate(["Service Desk", "Network", "Database", "Hardware", "Applications"])
    ]
    cis = [
        {
            "sys_id": _sys_id("ci", i),
            "name": f"{kind}-{i:03d}",
            "sys_class_name": kind,
            "operational_status": rnd.choice(["Operational", "Non-Operational"]),
            "owned_by": _sys_id("user", rnd.randrange(40)),
            "location": rnd.choice(["Chennai", "Pune", "Delhi"]),
        }
        for i, kind in enumerate(rnd.choice(["cmdb_ci_server", "cmdb_ci_appl", "cmdb_ci_netgear"]) for _ in range(15))
    ]
    problems = [
        {
            "sys_id": _sys_id("problem", i),
            "number": f"PRB{i + 1:07d}",
            "short_description": f"Recurring failure {i}",
            "state": "Root Cause Analysis",
            "priority": "2",
            "assigned_to": _sys_id("user", rnd.randrange(40)),
            "cmdb_ci": _sys_id("ci", rnd.randrange(15)),
            "opened_at": "2026-08-01 10:00:00",
        }
        for i in range(20)
    ]
    changes = [
        {
            "sys_id": _sys_id("change", i),
            "number": f"CHG{i + 1:07d}",
            "short_description": f"Patch {i}",
            "state": "Scheduled",
            "type": "Normal",
            "risk": "Moderate",
            "assigned_to": _sys_id("user", rnd.randrange(40)),
            "assignment_group": _sys_id("group", i % 5),
            "cmdb_ci": _sys_id("ci", i % 15),
            "start_date": "2026-09-20 22:00:00",
            "end_date": "2026-09-21 02:00:00",
        }
        for i in range(30)
    ]
    incidents = []
    for i in range(260):
        day = dt.date(2026, 9, 1) + dt.timedelta(days=i % 45)
        incidents.append(
            {
                "sys_id": _sys_id("incident", i),
                "number": f"INC{i + 1:07d}",
                "short_description": rnd.choice(
                    ["Email down", "VPN slow", "Laptop broken", "Printer jam", "App error"]
                ),
                "state": rnd.choice(["1", "2", "6"]),
                "priority": rnd.choice(["2", "3", "4"]),
                "category": rnd.choice(["network", "hardware", "software"]),
                "caller_id": _sys_id("user", rnd.randrange(40)),
                "assigned_to": _sys_id("user", rnd.randrange(40)),
                "assignment_group": _sys_id("group", rnd.randrange(5)),
                "cmdb_ci": _sys_id("ci", rnd.randrange(15)),
                "problem_id": _sys_id("problem", rnd.randrange(20)) if i % 4 == 0 else "",
                "opened_at": f"{day} 09:{i % 60:02d}:00",
                "resolved_at": "",
                "sys_updated_on": f"{day} 12:00:00",
                "sys_created_on": f"{day} 09:00:00",
            }
        )
    articles = [
        {
            "sys_id": _sys_id("kb", i),
            "number": f"KB{i + 1:07d}",
            "short_description": title,
            "text": body,
            "kb_category": cat,
            "workflow_state": state,
            "sys_updated_on": updated,
        }
        for i, (title, body, cat, state, updated) in enumerate(
            [
                (
                    "Reset your VPN token",
                    "<p>Open the <b>Self-service portal</b>.</p><ol><li>Choose VPN</li>"
                    "<li>Click <i>Reset token</i></li></ol><p>The new token works after 5 minutes.</p>",
                    "Network",
                    "published",
                    "2026-09-10 10:00:00",
                ),
                (
                    "Printer jam on floor 3",
                    "<p>Open tray 2 and remove the paper. Call extension 4455 if it persists.</p>",
                    "Hardware",
                    "published",
                    "2026-09-12 10:00:00",
                ),
                (
                    "Password policy",
                    "<p>Passwords expire every 90 days and need 14 characters.</p><script>alert(1)</script>",
                    "Security",
                    "published",
                    "2026-09-15 10:00:00",
                ),
                (
                    "Laptop refresh cycle",
                    "<p>Laptops are replaced every 4 years.</p><table><tr><td>Model</td>"
                    "<td>Years</td></tr><tr><td>Standard</td><td>4</td></tr></table>",
                    "Hardware",
                    "published",
                    "2026-10-02 10:00:00",
                ),
                ("Draft: new email client", "<p>Not yet published.</p>", "Software", "draft", "2026-10-03 10:00:00"),
            ]
        )
    ]
    for _table, rows in (
        ("sys_user", users),
        ("sys_user_group", groups),
        ("cmdb_ci", cis),
        ("problem", problems),
        ("change_request", changes),
    ):
        for r in rows:
            r.setdefault("sys_updated_on", "2026-09-01 08:00:00")
    return {
        "sys_user": users,
        "sys_user_group": groups,
        "cmdb_ci": cis,
        "problem": problems,
        "change_request": changes,
        "incident": incidents,
        "kb_knowledge": articles,
    }


SN_REFERENCES = {
    "caller_id": "sys_user",
    "assigned_to": "sys_user",
    "owned_by": "sys_user",
    "manager": "sys_user",
    "assignment_group": "sys_user_group",
    "cmdb_ci": "cmdb_ci",
    "problem_id": "problem",
}
SN_CHOICES = {
    "state": {"1": "New", "2": "In Progress", "6": "Resolved"},
    "priority": {"2": "2 - High", "3": "3 - Moderate", "4": "4 - Low"},
}


class MockServiceNow:
    def __init__(self):
        self.data = servicenow_data()
        self.requests: list[dict] = []
        self.throttle_next = 0
        self.tokens: set[str] = set()
        self.app = self._build()

    def _authorized(self, request: Request) -> bool:
        h = request.headers.get("authorization", "")
        if h.startswith("Basic "):
            return base64.b64decode(h[6:]).decode() == f"{SN_USER}:{SN_PASSWORD}"
        return h.startswith("Bearer ") and h[7:] in self.tokens

    def _display(self, field: str, value: str) -> str:
        if field in SN_REFERENCES and value:
            target = next((r for r in self.data[SN_REFERENCES[field]] if r["sys_id"] == value), None)
            return (target or {}).get("name") or (target or {}).get("number") or ""
        return SN_CHOICES.get(field, {}).get(value, value)

    def _build(self) -> FastAPI:
        app = FastAPI()

        @app.post("/oauth_token.do")
        def token(grant_type: str = Form(...), client_id: str = Form(...), client_secret: str = Form(...)):
            if (grant_type, client_id, client_secret) != ("client_credentials", SN_CLIENT, SN_CLIENT_SECRET):
                return JSONResponse({"error": "access_denied"}, status_code=401)
            t = uuid.uuid4().hex
            self.tokens.add(t)
            return {"access_token": t, "token_type": "Bearer", "expires_in": 1799}

        @app.get("/api/now/table/{table}")
        def table(table: str, request: Request):
            q = request.query_params
            self.requests.append({"table": table, **dict(q)})
            if self.throttle_next:
                self.throttle_next -= 1
                return JSONResponse({"error": "rate limited"}, status_code=429, headers={"Retry-After": "0"})
            if not self._authorized(request):
                return JSONResponse({"error": {"message": "User Not Authenticated"}}, status_code=401)
            if table not in self.data:
                return JSONResponse({"error": {"message": "Invalid table " + table}}, status_code=404)
            rows = list(self.data[table])
            for part in (q.get("sysparm_query") or "").split("^"):
                m = re.match(r"sys_updated_on>=javascript:gs\.dateGenerate\('([\d-]+)','([\d:]+)'\)", part)
                if m:
                    rows = [r for r in rows if r["sys_updated_on"] >= f"{m.group(1)} {m.group(2)}"]
                elif "=" in part and not part.upper().startswith("ORDERBY"):
                    k, v = part.split("=", 1)
                    rows = [r for r in rows if str(r.get(k)) == v]
            total = len(rows)
            offset, limit = int(q.get("sysparm_offset", 0)), int(q.get("sysparm_limit", 10000))
            page = rows[offset : offset + limit]
            fields = [f for f in (q.get("sysparm_fields") or "").split(",") if f]
            result = []
            for r in page:
                keys = fields or list(r)
                if q.get("sysparm_display_value") == "all":
                    result.append(
                        {k: {"display_value": self._display(k, r.get(k, "")), "value": r.get(k, "")} for k in keys}
                    )
                else:
                    result.append({k: r.get(k, "") for k in keys})
            return JSONResponse({"result": result}, headers={"X-Total-Count": str(total)})

        return app


# ------------------------------------------------------------------ SAP data
def _ms(moment: dt.datetime) -> int:
    """OData v2 /Date(ms)/ counts milliseconds since 1970-01-01 UTC."""
    return int(moment.replace(tzinfo=dt.UTC).timestamp() * 1000)


def _odata_date(day: dt.date) -> str:
    return f"/Date({_ms(dt.datetime(day.year, day.month, day.day))})/"


def sap_data(seed: int = 11) -> dict:
    rnd = random.Random(seed)
    partners = [
        {
            "BusinessPartner": f"171000{i:02d}",
            "BusinessPartnerFullName": f"Customer {i} Pvt Ltd",
            "BusinessPartnerCategory": "2",
            "CreationDate": _odata_date(dt.date(2025, 1, 1 + i % 28)),
            "Industry": "Retail",
        }
        for i in range(30)
    ]
    products = [
        {
            "Product": f"MAT-{i:04d}",
            "ProductType": "FERT",
            "ProductGroup": f"G{i % 4}",
            "BaseUnit": "EA",
            "CreationDate": _odata_date(dt.date(2024, 6, 1)),
        }
        for i in range(40)
    ]
    orders, items = [], []
    for i in range(120):
        changed = dt.datetime(2026, 9, 1) + dt.timedelta(days=i % 40)
        orders.append(
            {
                "SalesOrder": f"{1000000 + i}",
                "SalesOrderType": "OR",
                "SoldToParty": rnd.choice(partners)["BusinessPartner"],
                "SalesOrganization": "1710",
                "CreationDate": _odata_date(changed.date()),
                "TotalNetAmount": f"{rnd.randint(100, 9000)}.50",
                "TransactionCurrency": "INR",
                "OverallSDProcessStatus": rnd.choice(["A", "B", "C"]),
                "LastChangeDateTime": f"/Date({_ms(changed)}+0000)/",
            }
        )
        for j in range(1 + i % 3):
            items.append(
                {
                    "SalesOrder": f"{1000000 + i}",
                    "SalesOrderItem": f"{(j + 1) * 10}",
                    "Material": rnd.choice(products)["Product"],
                    "RequestedQuantity": str(rnd.randint(1, 20)),
                    "RequestedQuantityUnit": "EA",
                    "NetAmount": f"{rnd.randint(10, 900)}.00",
                    "Plant": "1710",
                }
            )
    return {
        "API_BUSINESS_PARTNER/A_BusinessPartner": partners,
        "API_SALES_ORDER_SRV/A_SalesOrder": orders,
        "API_SALES_ORDER_SRV/A_SalesOrderItem": items,
        "API_PRODUCT_SRV/A_Product": products,
    }


class MockSap:
    page_size = 50

    def __init__(self):
        self.data = sap_data()
        self.requests: list[dict] = []
        self.app = self._build()

    def _check(self, request: Request) -> JSONResponse | None:
        h = request.headers.get("authorization", "")
        ok = h.startswith("Basic ") and base64.b64decode(h[6:]).decode() == f"{SAP_USER}:{SAP_PASSWORD}"
        if not ok or request.query_params.get("sap-client") != SAP_CLIENT:
            return JSONResponse({"error": {"message": {"value": "Logon failed"}}}, status_code=401)
        return None

    @staticmethod
    def _filter(rows, expr: str):
        for part in [p.strip("() ") for p in re.split(r"\s+and\s+", expr or "") if p.strip()]:
            m = re.match(r"(\w+) ge datetimeoffset'([\d\-T:]+)Z?'", part)
            if m:
                since = _ms(dt.datetime.fromisoformat(m.group(2)))
                rows = [r for r in rows if int(re.search(r"\d+", r[m.group(1)]).group()) >= since]
                continue
            m = re.match(r"(\w+) eq '([^']*)'", part)
            if m:
                rows = [r for r in rows if r.get(m.group(1)) == m.group(2)]
        return rows

    def _build(self) -> FastAPI:
        app = FastAPI()

        @app.get("/sap/opu/odata/sap/{service}/{entity}")
        def v2(service: str, entity: str, request: Request):
            q = request.query_params
            self.requests.append({"path": f"{service}/{entity}", **dict(q)})
            denied = self._check(request)
            if denied:
                return denied
            key = f"{service}/{entity}"
            if key not in self.data:
                return JSONResponse(
                    {"error": {"message": {"value": f"Resource not found for {entity}"}}}, status_code=404
                )
            rows = self._filter(self.data[key], q.get("$filter"))
            start = int(q.get("$skiptoken") or q.get("$skip") or 0)
            want = int(q.get("$top") or len(rows))
            page = rows[start : start + min(want, self.page_size)]
            select = [f for f in (q.get("$select") or "").split(",") if f]
            base = str(request.url).split("?")[0]
            results = []
            for r in page:
                rec = {"__metadata": {"id": f"{base}('{list(r.values())[0]}')", "type": f"{service}.{entity}Type"}}
                rec.update({k: v for k, v in r.items() if not select or k in select})
                if not select:
                    rec["to_Item"] = {"__deferred": {"uri": f"{base}('x')/to_Item"}}
                results.append(rec)
            d = {"results": results}
            if len(page) == self.page_size and start + len(page) < len(rows) and want > self.page_size:
                params = {k: v for k, v in q.items() if k not in ("$skip", "$top", "$skiptoken")}
                tail = "&".join(f"{k}={v}" for k, v in params.items())
                d["__next"] = f"{base}?$skiptoken={start + len(page)}&{tail}"
            return {"d": d}

        @app.get("/sap/opu/odata4/sap/api_product/srvd_a2x/sap/product/0001/Product")
        def v4(request: Request):
            denied = self._check(request)
            if denied:
                return denied
            q = request.query_params
            rows = self.data["API_PRODUCT_SRV/A_Product"]
            start = int(q.get("$skiptoken") or q.get("$skip") or 0)
            page = rows[start : start + 25]
            body = {
                "@odata.context": "$metadata#Product",
                "value": [
                    {
                        **{k: v for k, v in r.items() if k != "CreationDate"},
                        "CreationDate": "2024-06-01",
                        "@odata.etag": 'W/"x"',
                    }
                    for r in page
                ],
            }
            if start + 25 < len(rows):
                body["@odata.nextLink"] = f"Product?$skiptoken={start + 25}&sap-client={SAP_CLIENT}"
            return body

        return app
