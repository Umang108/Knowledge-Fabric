"""Shared plumbing for source-system connectors (ServiceNow, SAP).

A connector reads tables from a remote system and returns plain rows. app/connectors/pipeline.py writes them
into a workbook (one sheet per table) or into text documents, and the normal TCS Knowledge Fabric pipelines take over:
LLM extraction + Review for graphs, chunk + embed for RAG, and add-data for later refreshes.
"""

import fnmatch
import logging
import re
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse

import httpx

from app.config import get_settings

log = logging.getLogger(__name__)

MAX_ROWS_DEFAULT = 50_000
RETRY_STATUS = {429, 502, 503, 504}


class ConnectorError(ValueError):
    """A problem the user can act on (bad URL, wrong password, unknown table...)."""


@dataclass
class Dataset:
    """One table to pull. `name` becomes the sheet name; `source` is the table / entity set."""

    name: str
    source: str
    fields: list[str] = field(default_factory=list)
    filter: str = ""
    limit: int | None = None
    changed_field: str = ""  # SAP: field holding the last-change timestamp, for "only data changed since"

    @classmethod
    def from_dict(cls, d: dict) -> "Dataset":
        if not isinstance(d, dict):
            raise ConnectorError("Each dataset must be an object")
        source = str(d.get("source") or "").strip()
        name = str(d.get("name") or source.split("/")[-1]).strip()
        fields = d.get("fields") or []
        if isinstance(fields, str):
            fields = [f.strip() for f in fields.split(",")]
        limit = d.get("limit")
        return cls(
            name=name,
            source=source,
            fields=[str(f).strip() for f in fields if str(f).strip()],
            filter=str(d.get("filter") or "").strip(),
            limit=int(limit) if limit not in (None, "") else None,
            changed_field=str(d.get("changed_field") or "").strip(),
        )

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "source": self.source,
            "fields": self.fields,
            "filter": self.filter,
            "limit": self.limit,
            "changed_field": self.changed_field,
        }


@dataclass
class Table:
    name: str
    columns: list[str]
    rows: list[dict]


def check_url(base_url: str) -> str:
    """Only http(s) URLs, and only hosts allowed by CONNECTOR_ALLOWED_HOSTS (if set)."""
    url = (base_url or "").strip().rstrip("/")
    parsed = urlparse(url)
    if parsed.scheme not in ("https", "http") or not parsed.hostname:
        raise ConnectorError("The URL must start with https:// (or http://) and include a host name")
    allowed = [h.strip().lower() for h in get_settings().connector_allowed_hosts.split(",") if h.strip()]
    if allowed and not any(fnmatch.fnmatch(parsed.hostname.lower(), pattern) for pattern in allowed):
        raise ConnectorError(f"{parsed.hostname} is not in CONNECTOR_ALLOWED_HOSTS")
    return url


def safe_sheet_name(name: str, taken: set[str]) -> str:
    """Excel sheet names: 1-31 characters, none of []:*?/\\ and unique."""
    base = re.sub(r"[\[\]:*?/\\]", "_", name).strip("' ")[:31] or "Sheet"
    candidate, n = base, 2
    while candidate.lower() in taken:
        suffix = f"_{n}"
        candidate, n = base[: 31 - len(suffix)] + suffix, n + 1
    taken.add(candidate.lower())
    return candidate


class Connector:
    kind = ""
    label = ""
    page_size = 1000

    def __init__(self, base_url: str, auth_type: str, username: str | None, secret: str | None, options: dict):
        self.base_url = check_url(base_url)
        self.auth_type = auth_type
        self.username = username or None
        self.secret = secret or None
        self.options = options or {}
        self.max_rows = int(self.options.get("max_rows") or MAX_ROWS_DEFAULT)
        s = get_settings()
        verify = s.connector_ca_bundle or True
        if self.options.get("verify_tls") is False:
            verify = False
        self._client = httpx.Client(timeout=httpx.Timeout(60, connect=15), verify=verify, follow_redirects=False)
        self._token: str | None = None
        self._token_expires = 0.0

    # -------------------------------------------------------------- to implement
    def presets(self) -> dict[str, list[dict]]:
        raise NotImplementedError

    def fetch_table(self, ds: Dataset, since: str | None = None, progress=None) -> Table:
        raise NotImplementedError

    def fetch_documents(self, ds: Dataset, since: str | None = None, progress=None) -> list[tuple[str, str]]:
        raise ConnectorError(f"{self.label} data can build a knowledge graph, not a RAG store")

    def validate_dataset(self, ds: Dataset) -> None:
        raise NotImplementedError

    # -------------------------------------------------------------- shared
    def close(self) -> None:
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def oauth_token(self) -> str:
        raise NotImplementedError

    def auth_headers(self) -> tuple[dict, httpx.Auth | None]:
        if self.auth_type == "basic":
            if not (self.username and self.secret):
                raise ConnectorError("Basic authentication needs a username and a password")
            return {}, httpx.BasicAuth(self.username, self.secret)
        if self.auth_type == "oauth":
            if not self._token or time.time() > self._token_expires - 60:
                self.oauth_token()
            return {"Authorization": f"Bearer {self._token}"}, None
        raise ConnectorError(f"Unknown authentication type {self.auth_type!r}")

    def request(self, method: str, url: str, **kw) -> httpx.Response:
        """HTTP with auth, retries on 429/5xx (honouring Retry-After) and readable errors."""
        extra = kw.pop("headers", {})
        for attempt in range(6):
            headers, auth = self.auth_headers()
            try:
                r = self._client.request(method, url, headers={**headers, **extra}, auth=auth, **kw)
            except httpx.TimeoutException as exc:
                if attempt == 5:
                    raise ConnectorError(f"{self.label} did not answer in time ({url})") from exc
                time.sleep(min(2**attempt, 30))
                continue
            except httpx.HTTPError as exc:
                raise ConnectorError(f"Cannot reach {self.label} at {self.base_url}: {exc}") from exc
            if r.status_code in RETRY_STATUS and attempt < 5:
                wait = r.headers.get("retry-after")
                time.sleep(min(float(wait) if wait and wait.replace(".", "").isdigit() else 2**attempt, 30))
                continue
            if r.status_code == 401 and self.auth_type == "oauth" and attempt == 0:
                self._token = None  # expired token: get a new one once
                continue
            if r.status_code in (401, 403):
                raise ConnectorError(
                    f"{self.label} refused the credentials (HTTP {r.status_code}). Check the user, password or "
                    "client secret, and that the account may read this data."
                )
            if r.status_code == 404:
                raise ConnectorError(
                    f"{self.label} has no {urlparse(url).path} (HTTP 404). Check the table or service."
                )
            if r.status_code >= 400:
                raise ConnectorError(f"{self.label} answered HTTP {r.status_code}: {r.text[:300]}")
            return r
        raise ConnectorError(f"{self.label} kept asking us to slow down; try again later")

    def test(self) -> dict:
        """Fetch one row of the first preset table, which proves URL, credentials and read access."""
        first = self.options.get("test_dataset") or next(iter(self.presets().values()))["datasets"][0]
        ds = Dataset.from_dict({**first, "limit": 1})
        table = self.fetch_table(ds)
        return {"ok": True, "detail": f"Connected: read {ds.source} ({len(table.columns)} columns)"}
