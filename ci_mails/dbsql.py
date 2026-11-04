"""Run SQL on the SQL warehouse the app uses (databricks.yml: medical-sql-wh-westus2), as the signed-in CLI user.

Auth comes from the Databricks CLI config (DATABRICKS_CONFIG_PROFILE picks a profile); DATABRICKS_WAREHOUSE_ID
overrides the warehouse.
"""
import os
import time

import requests
from databricks.sdk import WorkspaceClient
from databricks.sdk.service.sql import Disposition, Format, StatementParameterListItem, StatementState

W = WorkspaceClient()
WAREHOUSE = os.environ.get("DATABRICKS_WAREHOUSE_ID") or next(
    w.id for w in W.warehouses.list() if w.name == "medical-sql-wh-westus2")


def sql(statement: str, params: dict | None = None) -> list[dict]:
    """Rows as {column: value}, every value a string (arrays and structs as JSON); :name parameters from params."""
    plist = [StatementParameterListItem(name=k, value=v) for k, v in (params or {}).items()] or None
    r = W.statement_execution.execute_statement(statement=statement, warehouse_id=WAREHOUSE, wait_timeout="50s",
                                                parameters=plist, disposition=Disposition.EXTERNAL_LINKS,
                                                format=Format.JSON_ARRAY)
    while r.status.state in (StatementState.PENDING, StatementState.RUNNING):
        time.sleep(2)
        r = W.statement_execution.get_statement(r.statement_id)
    if r.status.state != StatementState.SUCCEEDED:
        raise RuntimeError(f"{r.status.state}: {r.status.error.message if r.status.error else ''}")
    cols = [c.name for c in r.manifest.schema.columns or []] if r.manifest and r.manifest.schema else []
    rows, chunk = [], r.result
    while chunk is not None:
        nxt = None
        for link in chunk.external_links or []:
            rows += requests.get(link.external_link, timeout=120).json()
            nxt = link.next_chunk_index
        if nxt is None:
            break
        chunk = W.statement_execution.get_statement_result_chunk_n(r.statement_id, nxt)
    return [dict(zip(cols, row)) for row in rows]
