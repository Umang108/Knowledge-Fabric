"""Connectors to source systems. Each pulls tables (and, for ServiceNow, knowledge articles) into the normal
Graphbase pipelines. See base.py for the shared plumbing and pipeline.py for how the data is handed over."""

from app.connectors.base import Connector, ConnectorError, Dataset, Table
from app.connectors.sap import SapODataConnector
from app.connectors.servicenow import ServiceNowConnector

KINDS: dict[str, type[Connector]] = {"servicenow": ServiceNowConnector, "sap": SapODataConnector}

__all__ = ["KINDS", "Connector", "ConnectorError", "Dataset", "Table", "SapODataConnector", "ServiceNowConnector"]
