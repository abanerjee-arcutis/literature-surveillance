"""Databricks App: builds the Literature Update page from the tables on every request (the articles and the last run
that the daily job wrote, the decisions and settings saved here), saves reviewers' Accept / Ignore / Restore clicks
(with the reasons ticked for an accept or ignore) to the decisions table, and saves the Settings dialog (sources,
search terms and priority keywords) to the settings table, so the next run uses them. The tables are
<LITSURV_TABLES>_articles, _runs, _decisions and _settings (tables.sql). Settings → L3 lists the L3 reasons and
push-down rules the last run scored with, from its row in _runs.

The app runs its SQL on a SQL warehouse as its own service principal, so viewers only need "Can use" on the app.
"""
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.sql import StatementParameterListItem, StatementState
from flask import Flask, Response, jsonify, request

from page import TEMPLATE, fill, header

TABLES = os.environ.get("LITSURV_TABLES", "medical_prod.apps.literature_surveillance")
WAREHOUSE_ID = os.environ.get("DATABRICKS_WAREHOUSE_ID")   # app.yaml takes it from the app's sql-warehouse resource
ARTICLES, RUNS = f"{TABLES}_articles", f"{TABLES}_runs"   # written by the daily job
DECISIONS, SETTINGS = f"{TABLES}_decisions", f"{TABLES}_settings"   # written here; surveillance.py reads both
DECISION_VALUES = ("accepted", "ignored", "restored", "removed", "")   # 'removed' is the older name for 'ignored'
TERM_FIELDS = ["term", "product_category", "pubmed_query", "openalex_query", "scholar_query"]
TERMS_TYPE = "ARRAY<STRUCT<" + ", ".join(f"{k}: STRING" for k in TERM_FIELDS) + ">>"
CATEGORIES = ("Roflumilast", "Competitor", "Class")
SOURCES = ("pubmed", "openalex", "scholar")

app = Flask(__name__)
w = WorkspaceClient()
pool = ThreadPoolExecutor(4)                           # the page's four queries run side by side
with open(TEMPLATE, encoding="utf-8") as f:
    template = f.read()


def sql(statement: str, **params) -> list[list]:
    """Runs one statement on the SQL warehouse with :name parameters; returns its rows, every value a string
    (arrays and structs as JSON). Waits up to two minutes, enough for a stopped warehouse to start."""
    r = w.statement_execution.execute_statement(
        statement=statement, warehouse_id=WAREHOUSE_ID, wait_timeout="30s",
        parameters=[StatementParameterListItem(name=k, value=v) for k, v in params.items()])
    deadline = time.time() + 120
    while r.status.state in (StatementState.PENDING, StatementState.RUNNING):
        if time.time() > deadline:
            w.statement_execution.cancel_execution(r.statement_id)
            raise TimeoutError("the SQL warehouse did not answer within two minutes")
        time.sleep(1)
        r = w.statement_execution.get_statement(r.statement_id)
    if r.status.state != StatementState.SUCCEEDED:
        raise RuntimeError(r.status.error.message if r.status.error else str(r.status.state))
    rows, chunk = [], r.result
    while chunk:
        rows += chunk.data_array or []
        chunk = (w.statement_execution.get_statement_result_chunk_n(r.statement_id, chunk.next_chunk_index)
                 if chunk.next_chunk_index else None)
    return rows


def read_last_run() -> dict | None:
    """The newest row of the runs table: when the job last ran, its search window and what it searched with."""
    rows = sql(f"SELECT to_json(struct(*)) FROM {RUNS} ORDER BY run_at DESC LIMIT 1")
    return json.loads(rows[0][0]) if rows else None


def read_page_articles() -> list[tuple]:
    """(id, [aliases], page row) of each article on the page, in page order."""
    rows = sql(f"SELECT id, to_json(aliases), page_row FROM {ARTICLES} WHERE page_position IS NOT NULL ORDER BY page_position")
    return [(k, json.loads(aliases or "[]"), json.loads(row)) for k, aliases, row in rows]


def read_decisions() -> dict:
    """{article id: (decision, [reasons])}."""
    rows = sql(f"SELECT article_id, decision, to_json(reasons) FROM {DECISIONS}")
    return {k: (d, json.loads(r) if r else []) for k, d, r in rows}


def read_settings() -> dict | None:
    """The newest saved settings, in the shape the page and surveillance.py use, or None if never saved."""
    rows = sql(f"SELECT to_json(struct(*)) FROM {SETTINGS} ORDER BY updated_at DESC LIMIT 1")
    return json.loads(rows[0][0]) if rows else None


def clean_reasons(reasons) -> list[str]:
    """The keywords and reasons the reviewer ticked, trimmed and without repeats."""
    if not isinstance(reasons, list) or len(reasons) > 40:
        raise ValueError("reasons must be a list of at most 40 labels")
    out = []
    for r in reasons:
        r = " ".join(str(r or "").replace(";", ",").split())[:80]
        if r and r not in out:
            out.append(r)
    return out


def clean_settings(body: dict) -> dict:
    """The settings the page sent, checked; raises ValueError with a message the page shows."""
    terms, seen = [], set()
    for t in body.get("terms") or []:
        row = {k: str(t.get(k) or "").strip() for k in TERM_FIELDS}
        if not row["term"] or not row["pubmed_query"]:
            raise ValueError("every term needs a name and a PubMed query")
        if row["product_category"] not in CATEGORIES:
            raise ValueError(f"{row['term']}: category must be one of {', '.join(CATEGORIES)}")
        if row["term"].lower() in seen:
            raise ValueError(f"{row['term']} is listed twice")
        seen.add(row["term"].lower())
        terms.append(row)
    if not terms:
        raise ValueError("at least one term is needed")
    sources = [s for s in SOURCES if s in (body.get("sources") or [])]
    if not sources:
        raise ValueError("at least one source must be switched on")
    try:
        pages = int(body.get("scholar_pages") or 1)
    except (TypeError, ValueError):
        raise ValueError("Google Scholar pages per term must be a number") from None
    if not 1 <= pages <= 5:
        raise ValueError("Google Scholar pages per term must be 1 to 5")
    keywords, seen = [], set()
    for k in body.get("priority_keywords") or []:
        k = str(k or "").strip()
        if k and k.lower() in seen:
            raise ValueError(f"L3 keyword {k} is listed twice")
        if k:
            seen.add(k.lower())
            keywords.append(k)
    return dict(terms=terms, sources=sources, scholar_pages=pages, priority_keywords=keywords)


@app.get("/")
def page():
    run, articles, decisions, saved = (f.result() for f in [pool.submit(read_last_run), pool.submit(read_page_articles),
                                                            pool.submit(read_decisions), pool.submit(read_settings)])
    if run is None:
        return Response(f"<p>No page yet: {RUNS} is empty. Run the daily job once (Jobs &amp; Pipelines) and reload.</p>",
                        status=503, mimetype="text/html")
    hook = {"DECISIONS_API": "api/decisions", "DECISIONS": {}, "REASONS": {}, "SETTINGS_API": "api/settings"}
    for key, aliases, _ in articles:                  # a decision stays with an article whose id changed, as in decision_for()
        decision, reasons = next((decisions[k] for k in (key, *aliases) if k in decisions), (None, []))
        if decision:
            hook["DECISIONS"][key] = decision
        if reasons:
            hook["REASONS"][key] = reasons
    if saved:
        hook["SAVED_SETTINGS"] = saved
    last = dict(terms=run["terms"], sources=run["sources"], scholar_pages=run["scholar_pages"],   # what the last run searched with
                priority_keywords=run.get("priority_keywords") or [], keys=run["api_keys"],
                l3_rules=json.loads(run.get("l3_rules") or "[]"))   # absent from runs before the L3 score
    html = fill(template, [row for _, _, row in articles], header(run["sources"], date.fromisoformat(run["window_start"]),
                date.fromisoformat(run["window_end"]), datetime.fromisoformat(run["run_at"])), last)
    script = "".join(f"window.{k} = {json.dumps(v)};" for k, v in hook.items()).replace("</", "<\\/")
    return Response(html.replace("</head>", f"<script>{script}</script></head>", 1), mimetype="text/html")


@app.post("/api/decisions")
def save_decision():
    body = request.get_json(silent=True) or {}
    article, decision = str(body.get("id") or ""), body.get("decision") or ""
    if not article or decision not in DECISION_VALUES:
        return jsonify(error="expected {id, decision: 'accepted' | 'ignored' | 'restored' | '', reasons: [...]}"), 400
    decision = "ignored" if decision == "removed" else decision
    try:
        reasons = clean_reasons(body.get("reasons") or []) if decision in ("accepted", "ignored") else []
    except ValueError as e:
        return jsonify(error=str(e)), 400
    if decision:
        sql(f"""MERGE INTO {DECISIONS} t USING (SELECT :id AS article_id) s ON t.article_id = s.article_id
                WHEN MATCHED THEN UPDATE SET decision = :decision, reasons = from_json(:reasons, 'ARRAY<STRING>'),
                                             decided_by = :by, decided_at = current_timestamp()
                WHEN NOT MATCHED THEN INSERT (article_id, decision, reasons, decided_by, decided_at)
                  VALUES (:id, :decision, from_json(:reasons, 'ARRAY<STRING>'), :by, current_timestamp())""",
            id=article, decision=decision, reasons=json.dumps(reasons), by=request.headers.get("X-Forwarded-Email", ""))
    else:                                              # undo back to "no decision"
        sql(f"DELETE FROM {DECISIONS} WHERE article_id = :id", id=article)
    return jsonify(ok=True)


@app.post("/api/settings")
def save_settings():
    try:
        settings = clean_settings(request.get_json(silent=True) or {})
    except ValueError as e:
        return jsonify(error=str(e)), 400
    settings.update(updated_by=request.headers.get("X-Forwarded-Email", ""),
                    updated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    sql(f"""INSERT INTO {SETTINGS} (updated_at, updated_by, sources, scholar_pages, priority_keywords, terms)
            VALUES (CAST(:updated_at AS TIMESTAMP), :updated_by, from_json(:sources, 'ARRAY<STRING>'), CAST(:scholar_pages AS INT),
                    from_json(:priority_keywords, 'ARRAY<STRING>'), from_json(:terms, '{TERMS_TYPE}'))""",
        updated_at=settings["updated_at"], updated_by=settings["updated_by"], sources=json.dumps(settings["sources"]),
        scholar_pages=str(settings["scholar_pages"]), priority_keywords=json.dumps(settings["priority_keywords"]),
        terms=json.dumps(settings["terms"]))
    return jsonify(ok=True, settings=settings)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("DATABRICKS_APP_PORT", 8000)))
