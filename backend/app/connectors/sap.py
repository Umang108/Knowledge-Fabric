"""SAP connector: OData services of S/4HANA (on-premise or Cloud) or SAP BTP.

    OData v2 (default): {base_url}{service_root}{SERVICE}/{EntitySet}, e.g.
        https://s4.example.com:44300/sap/opu/odata/sap/API_SALES_ORDER_SRV/A_SalesOrder
    OData v4: set odata_version "4" and give the service path, e.g.
        api_salesorder/srvd_a2x/sap/salesorder/0001/SalesOrder   (under /sap/opu/odata4/sap/)

Authentication: Basic (communication user + password) or OAuth client credentials (token_url, client id and
secret, e.g. an SAP BTP service key). Options: sap_client (adds ?sap-client=NNN), service_root, odata_version.

Paging follows the server (`__next` in v2, `@odata.nextLink` in v4) and falls back to $top/$skip.
Values are cleaned for the graph: `__metadata` and deferred navigation links are dropped, `/Date(...)/`
becomes an ISO date, `PT12H30M00S` becomes 12:30:00, and complex types are flattened (Address_City).
"""

import datetime as dt
import re
import time
from urllib.parse import urljoin

from app.connectors.base import Connector, ConnectorError, Dataset, Table

SOURCE = re.compile(r"^/?[A-Za-z0-9_./;=',()\-]{1,300}$")
FIELD = re.compile(r"^[A-Za-z0-9_/]{1,120}$")
V2_DATE = re.compile(r"^/Date\((-?\d+)([+-]\d{4})?\)/$")
DURATION = re.compile(r"^PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)(?:\.\d+)?S)?$")

PRESETS = {
    "sales": {
        "label": "Sales (business partners, sales orders and items, products)",
        "kb_type": "graph",
        "datasets": [
            {
                "name": "BusinessPartners",
                "source": "API_BUSINESS_PARTNER/A_BusinessPartner",
                "fields": ["BusinessPartner", "BusinessPartnerFullName", "BusinessPartnerCategory", "CreationDate"],
            },
            {
                "name": "SalesOrders",
                "source": "API_SALES_ORDER_SRV/A_SalesOrder",
                "fields": [
                    "SalesOrder",
                    "SalesOrderType",
                    "SoldToParty",
                    "SalesOrganization",
                    "CreationDate",
                    "TotalNetAmount",
                    "TransactionCurrency",
                    "OverallSDProcessStatus",
                ],
                "changed_field": "LastChangeDateTime",
            },
            {
                "name": "SalesOrderItems",
                "source": "API_SALES_ORDER_SRV/A_SalesOrderItem",
                "fields": [
                    "SalesOrder",
                    "SalesOrderItem",
                    "Material",
                    "RequestedQuantity",
                    "RequestedQuantityUnit",
                    "NetAmount",
                    "Plant",
                ],
            },
            {
                "name": "Products",
                "source": "API_PRODUCT_SRV/A_Product",
                "fields": ["Product", "ProductType", "ProductGroup", "BaseUnit", "CreationDate"],
            },
        ],
    },
    "procurement": {
        "label": "Procurement (suppliers, purchase orders and items)",
        "kb_type": "graph",
        "datasets": [
            {
                "name": "Suppliers",
                "source": "API_BUSINESS_PARTNER/A_Supplier",
                "fields": ["Supplier", "SupplierName", "CreationDate"],
            },
            {
                "name": "PurchaseOrders",
                "source": "API_PURCHASEORDER_PROCESS_SRV/A_PurchaseOrder",
                "fields": [
                    "PurchaseOrder",
                    "Supplier",
                    "PurchaseOrderType",
                    "CompanyCode",
                    "PurchasingOrganization",
                    "CreationDate",
                ],
            },
            {
                "name": "PurchaseOrderItems",
                "source": "API_PURCHASEORDER_PROCESS_SRV/A_PurchaseOrderItem",
                "fields": [
                    "PurchaseOrder",
                    "PurchaseOrderItem",
                    "Material",
                    "OrderQuantity",
                    "NetPriceAmount",
                    "Plant",
                ],
            },
        ],
    },
}


def _value(v):
    if isinstance(v, str):
        m = V2_DATE.match(v)
        if m:
            moment = dt.datetime(1970, 1, 1) + dt.timedelta(milliseconds=int(m.group(1)))
            return moment.date().isoformat() if moment.time() == dt.time() else moment.isoformat(timespec="seconds")
        m = DURATION.match(v)
        if m and v != "PT":
            h, mi, s = (int(x or 0) for x in m.groups())
            return f"{h:02d}:{mi:02d}:{s:02d}"
    return v


def flatten(record: dict, prefix: str = "") -> dict:
    out = {}
    for key, value in record.items():
        if key.startswith("__") or key.startswith("@odata"):
            continue
        if isinstance(value, dict):
            if "__deferred" in value or "results" in value or "__metadata" in value and len(value) == 1:
                continue  # navigation property (not expanded, or an expanded collection)
            out.update(flatten(value, f"{prefix}{key}_"))
        elif isinstance(value, list):
            continue  # expanded collection: pull it as its own dataset instead
        else:
            out[f"{prefix}{key}"] = _value(value)
    return out


class SapODataConnector(Connector):
    kind = "sap"
    label = "SAP"
    PRESETS = PRESETS
    page_size = 500

    @property
    def version(self) -> str:
        return str(self.options.get("odata_version") or "2")

    def presets(self) -> dict:
        return self.PRESETS

    def validate_dataset(self, ds: Dataset) -> None:
        if not SOURCE.match(ds.source) or ".." in ds.source:
            raise ConnectorError(f"{ds.source!r} is not an OData path like API_SALES_ORDER_SRV/A_SalesOrder")
        bad = [f for f in ds.fields + ([ds.changed_field] if ds.changed_field else []) if not FIELD.match(f)]
        if bad:
            raise ConnectorError(f"Invalid field name(s) for {ds.source}: {', '.join(bad)}")

    def oauth_token(self) -> str:
        url, client_id = self.options.get("token_url"), self.options.get("client_id")
        if not (url and client_id and self.secret):
            raise ConnectorError("OAuth needs a token URL, client id and client secret (e.g. from a BTP service key)")
        r = self._client.post(url, data={"grant_type": "client_credentials"}, auth=(client_id, self.secret))
        if r.status_code != 200:
            raise ConnectorError(f"The OAuth server refused the client (HTTP {r.status_code}): {r.text[:200]}")
        body = r.json()
        self._token, self._token_expires = body["access_token"], time.time() + int(body.get("expires_in", 1800))
        return self._token

    def entity_url(self, ds: Dataset) -> str:
        if ds.source.startswith("/"):
            return f"{self.base_url}{ds.source}"
        default_root = "/sap/opu/odata4/sap/" if self.version == "4" else "/sap/opu/odata/sap/"
        root = "/" + str(self.options.get("service_root") or default_root).strip("/") + "/"
        return f"{self.base_url}{root}{ds.source}"

    def _filter(self, ds: Dataset, since: str | None) -> str:
        parts = [f"({ds.filter})"] if ds.filter else []
        if since:
            if not ds.changed_field:
                raise ConnectorError(f"{ds.name}: set its changed field (e.g. LastChangeDateTime) to pull changes only")
            stamp = since.replace(" ", "T")
            stamp = stamp if "T" in stamp else f"{stamp}T00:00:00"
            stamp = stamp if stamp.endswith("Z") else f"{stamp}Z"
            literal = f"datetimeoffset'{stamp}'" if self.version == "2" else stamp
            parts.append(f"{ds.changed_field} ge {literal}")
        return " and ".join(parts)

    def _pages(self, ds: Dataset, since: str | None, progress=None):
        cap = min(ds.limit or self.max_rows, self.max_rows)
        url = self.entity_url(ds)
        base_params = {}
        if self.version == "2":
            base_params["$format"] = "json"
        if self.options.get("sap_client"):
            base_params["sap-client"] = str(self.options["sap_client"])
        if ds.fields:
            base_params["$select"] = ",".join(ds.fields)
        flt = self._filter(ds, since)
        if flt:
            base_params["$filter"] = flt
        fetched, skip, next_url = 0, 0, None
        while fetched < cap:
            top = min(self.page_size, cap - fetched)
            if next_url:
                r = self.request("GET", next_url, headers={"Accept": "application/json"})
            else:
                params = {**base_params, "$top": top, "$skip": skip}
                r = self.request("GET", url, params=params, headers={"Accept": "application/json"})
            try:
                body = r.json()
            except ValueError as exc:
                raise ConnectorError(f"SAP did not return JSON for {ds.source}: {r.text[:200]}") from exc
            if self.version == "2":
                d = body.get("d", body)
                rows = d.get("results", []) if isinstance(d, dict) else d
                nxt = d.get("__next") if isinstance(d, dict) else None
            else:
                rows, nxt = body.get("value", []), body.get("@odata.nextLink")
            rows = rows[: cap - fetched]
            yield rows
            fetched += len(rows)
            if progress:
                progress(f"{ds.source}: {fetched:,} rows")
            if nxt:
                next_url = urljoin(r.url.__str__(), nxt)
                if self.options.get("sap_client") and "sap-client=" not in next_url:
                    next_url += ("&" if "?" in next_url else "?") + f"sap-client={self.options['sap_client']}"
                continue
            next_url = None
            if len(rows) < top:
                break
            skip += len(rows)

    def fetch_table(self, ds: Dataset, since: str | None = None, progress=None) -> Table:
        self.validate_dataset(ds)
        rows, columns = [], {}
        for page in self._pages(ds, since, progress):
            for record in page:
                flat = flatten(record)
                rows.append(flat)
                for k in flat:
                    columns.setdefault(k, None)
        cols = [f for f in ds.fields if f in columns] + [c for c in columns if c not in ds.fields]
        if not cols and ds.fields:
            cols = list(ds.fields)
        return Table(ds.name, cols, rows)
