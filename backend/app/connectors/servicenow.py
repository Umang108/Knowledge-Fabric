"""ServiceNow connector: the Table API (GET /api/now/table/{table}).

Authentication: Basic (user + password) or OAuth client credentials (client id + secret; token from
/oauth_token.do). The account needs read access (e.g. the itil role, or an ACL-scoped integration user).

Rows are read with sysparm_display_value=all, so each field arrives as {"value", "display_value"}:
  * reference fields (value is a sys_id) become two columns: `caller_id` = the sys_id, which matches
    sys_user.sys_id so the graph can link them, and `caller_id_name` = the readable name;
  * choice fields keep their label ("In Progress", not "2"); dates keep the stored UTC value.
Knowledge articles (kb_knowledge) can feed a RAG store: the HTML body becomes plain text documents.
"""

import re
import time
from html.parser import HTMLParser

from app.connectors.base import Connector, ConnectorError, Dataset, Table

SYS_ID = re.compile(r"^[0-9a-f]{32}$")
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}( \d{2}:\d{2}:\d{2})?$")
TABLE = re.compile(r"^[a-z0-9_]{1,80}$")
FIELD = re.compile(r"^[A-Za-z0-9_.]{1,120}$")

PRESETS = {
    "itsm": {
        "label": "IT service management (incidents, problems, changes, users, groups, CIs)",
        "kb_type": "graph",
        "datasets": [
            {
                "name": "Incidents",
                "source": "incident",
                "fields": [
                    "sys_id",
                    "number",
                    "short_description",
                    "state",
                    "priority",
                    "category",
                    "caller_id",
                    "assigned_to",
                    "assignment_group",
                    "cmdb_ci",
                    "problem_id",
                    "opened_at",
                    "resolved_at",
                ],
            },
            {
                "name": "Problems",
                "source": "problem",
                "fields": [
                    "sys_id",
                    "number",
                    "short_description",
                    "state",
                    "priority",
                    "assigned_to",
                    "cmdb_ci",
                    "opened_at",
                ],
            },
            {
                "name": "Changes",
                "source": "change_request",
                "fields": [
                    "sys_id",
                    "number",
                    "short_description",
                    "state",
                    "type",
                    "risk",
                    "assigned_to",
                    "assignment_group",
                    "cmdb_ci",
                    "start_date",
                    "end_date",
                ],
            },
            {
                "name": "Users",
                "source": "sys_user",
                "fields": ["sys_id", "user_name", "name", "email", "title", "department", "manager"],
            },
            {"name": "Groups", "source": "sys_user_group", "fields": ["sys_id", "name", "manager", "description"]},
            {
                "name": "ConfigurationItems",
                "source": "cmdb_ci",
                "fields": ["sys_id", "name", "sys_class_name", "operational_status", "owned_by", "location"],
            },
        ],
    },
    "knowledge": {
        "label": "Knowledge articles (published) for a RAG store",
        "kb_type": "rag",
        "datasets": [
            {
                "name": "Knowledge",
                "source": "kb_knowledge",
                "fields": ["sys_id", "number", "short_description", "text", "kb_category", "sys_updated_on"],
                "filter": "workflow_state=published",
            },
        ],
    },
}


class _Text(HTMLParser):
    BREAKS = {"br", "p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "table", "ul", "ol"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        elif tag in self.BREAKS:
            self.parts.append("\n")
        if tag == "li":
            self.parts.append("- ")
        if tag in ("td", "th"):
            self.parts.append(" | ")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._skip = max(0, self._skip - 1)
        elif tag in self.BREAKS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    p = _Text()
    p.feed(html or "")
    text = "".join(p.parts)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    return re.sub(r"\n\s*\n\s*\n+", "\n\n", "\n".join(line.strip() for line in text.split("\n"))).strip()


def since_query(since: str) -> str:
    date, _, clock = since.replace("T", " ").partition(" ")
    return f"sys_updated_on>=javascript:gs.dateGenerate('{date}','{(clock or '00:00:00')[:8]}')"


class ServiceNowConnector(Connector):
    kind = "servicenow"
    label = "ServiceNow"
    PRESETS = PRESETS

    def presets(self) -> dict:
        return self.PRESETS

    def validate_dataset(self, ds: Dataset) -> None:
        if not TABLE.match(ds.source):
            raise ConnectorError(f"{ds.source!r} is not a ServiceNow table name (e.g. incident, sys_user)")
        bad = [f for f in ds.fields if not FIELD.match(f)]
        if bad:
            raise ConnectorError(f"Invalid field name(s) for {ds.source}: {', '.join(bad)}")

    def oauth_token(self) -> str:
        client_id = self.options.get("client_id")
        if not (client_id and self.secret):
            raise ConnectorError("OAuth needs the client id and client secret of a ServiceNow OAuth application")
        url = self.options.get("token_url") or f"{self.base_url}/oauth_token.do"
        r = self._client.post(
            url, data={"grant_type": "client_credentials", "client_id": client_id, "client_secret": self.secret}
        )
        if r.status_code != 200:
            raise ConnectorError(f"ServiceNow refused the OAuth client (HTTP {r.status_code}): {r.text[:200]}")
        body = r.json()
        self._token, self._token_expires = body["access_token"], time.time() + int(body.get("expires_in", 1800))
        return self._token

    # -------------------------------------------------------------- reading
    def _pages(self, ds: Dataset, since: str | None, progress=None):
        query = "^".join(q for q in (ds.filter, since_query(since) if since else "") if q)
        if "ORDERBY" not in query.upper():
            query = f"{query}^ORDERBYsys_created_on" if query else "ORDERBYsys_created_on"
        fields = list(dict.fromkeys(["sys_id", *ds.fields])) if ds.fields else []
        cap = min(ds.limit or self.max_rows, self.max_rows)
        offset, total = 0, None
        while offset < cap:
            size = min(self.page_size, cap - offset)
            params = {
                "sysparm_display_value": "all",
                "sysparm_exclude_reference_link": "true",
                "sysparm_limit": size,
                "sysparm_offset": offset,
                "sysparm_query": query,
            }
            if fields:
                params["sysparm_fields"] = ",".join(fields)
            r = self.request(
                "GET",
                f"{self.base_url}/api/now/table/{ds.source}",
                params=params,
                headers={"Accept": "application/json"},
            )
            rows = r.json().get("result", [])
            if total is None and r.headers.get("x-total-count", "").isdigit():
                total = int(r.headers["x-total-count"])
            yield rows
            offset += len(rows)
            if progress:
                progress(
                    f"{ds.source}: {offset:,}" + (f" of {min(total, cap):,}" if total is not None else "") + " rows"
                )
            if len(rows) < size or (total is not None and offset >= total):
                break

    @staticmethod
    def _flatten(record: dict) -> dict:
        out = {}
        for key, cell in record.items():
            if isinstance(cell, dict):
                value, display = cell.get("value", ""), cell.get("display_value", "")
                if isinstance(value, str) and SYS_ID.match(value) and key != "sys_id" and display != value:
                    out[key], out[f"{key}_name"] = value, display  # reference: id to link on + readable name
                elif isinstance(value, str) and DATE.match(value):
                    out[key] = value
                else:
                    out[key] = display if display not in (None, "") else value
            else:
                out[key] = cell
        return {k: (None if v == "" else v) for k, v in out.items()}

    def fetch_table(self, ds: Dataset, since: str | None = None, progress=None) -> Table:
        self.validate_dataset(ds)
        rows, columns = [], {}
        for page in self._pages(ds, since, progress):
            for record in page:
                flat = self._flatten(record)
                rows.append(flat)
                for k in flat:
                    columns.setdefault(k, None)
        order = list(dict.fromkeys(["sys_id", *ds.fields])) if ds.fields else list(columns)
        cols = []
        for f in order:  # requested order, each reference followed by its _name column
            if f in columns:
                cols.append(f)
            if f"{f}_name" in columns:
                cols.append(f"{f}_name")
        cols += [c for c in columns if c not in cols]
        return Table(ds.name, cols, rows)

    def fetch_documents(self, ds: Dataset, since: str | None = None, progress=None) -> list[tuple[str, str]]:
        """Knowledge articles as (file name, text): title, number and the article body as plain text."""
        table = self.fetch_table(ds, since, progress)
        docs, used = [], set()
        for row in table.rows:
            title = str(row.get("short_description") or row.get("number") or row.get("sys_id"))
            body = html_to_text(str(row.get("text") or ""))
            if not body.strip():
                continue
            stem = re.sub(r"[^\w\- ]+", "", f"{row.get('number') or ''} {title}").strip()[:80] or row["sys_id"]
            name, n = f"{stem}.md", 2
            while name.lower() in used:
                name, n = f"{stem} ({n}).md", n + 1
            used.add(name.lower())
            header = f"# {title}\n\nArticle {row.get('number') or ''}"
            if row.get("kb_category"):
                header += f" · Category: {row['kb_category']}"
            docs.append((name, f"{header}\n\n{body}\n"))
        return docs
