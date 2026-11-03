# Literature Surveillance – target stack and data schema

Status: proposal, 24 Sep 2026. Describes how the current working model (`surveillance.py` + `template.html`,
flat files in `data/`) moves onto the org's Databricks workspace, using Unity Catalog tables and volumes as
the database. `<catalog>` stands for the Unity Catalog catalog we are given; the schema is `lit_surveillance`.

## 1. Where we are now

| Piece | Today |
|---|---|
| Fetch, tag, summarise | `surveillance.py`, run by hand |
| Store | `data/articles.json`, one record per PMID |
| Search terms, exclusion rules | `terms.csv`, `exclusions.csv` |
| Reviewer decisions | `data/decisions.csv` (optional; the page's Remove/Restore buttons last one browser session only) |
| Page | `literature_update.html`, rendered from `template.html` with the data inlined |
| Interim Databricks App | `app.py` + `app.yaml`: serves the page and re-runs the script on demand. The app's disk resets on every restart or redeploy, so this is a wireframe host only |

## 2. Target stack

| Layer | Choice | Reason |
|---|---|---|
| Fetching from PubMed | Scheduled **Databricks Job** (serverless, daily) running a job version of `surveillance.py` | Fetching comes out of the app; runs are logged and retried by the platform |
| Raw files | **Unity Catalog volume** `<catalog>.lit_surveillance.raw` for PubMed/PMC XML | Re-parse after a parser fix without calling PubMed again; audit of what was received |
| Database | **Unity Catalog Delta tables** in `<catalog>.lit_surveillance`, read through a **SQL warehouse** | Governed, queryable from notebooks and dashboards; scale (hundreds of articles, a few reviewers) is small |
| App backend | **FastAPI** + `databricks-sql-connector`, deployed as a **Databricks App** | Same language as the existing code; small JSON API |
| Frontend | **Existing `template.html`**, with the hard-coded `const DATA = [...]` replaced by `fetch('/api/articles')` and Remove/Restore calling the API | Keeps the reviewed wireframe as is |
| Identity | Databricks App sign-in; the app reads the viewer's email from the `X-Forwarded-Email` header | Every reviewer decision is attributed to a person |
| Secrets | Databricks secret scope `lit-surveillance`, key `pubmed-api-key` (NCBI key), read by the job | Nothing sensitive in the repo |
| Deployment | **Databricks Asset Bundle** (`databricks.yml`) defining the app, the job and permissions; **GitHub Actions** deploys on push to `main` | Push-to-deploy; the whole setup is kept in the repo |

Not chosen:
- **Streamlit or Dash:** either would mean rebuilding a UI that has already been reviewed, and they handle card-and-filter pages worse than plain HTML/JS.
- **React:** only worth it if the UI grows well beyond the current page.

### Data flow

```
             daily                      MERGE / overwrite
PubMed ──► Databricks Job ──────────────► articles, article_terms, articles_enriched, runs
 (E-utilities)   │                                         │
                 └──► volume raw/ (XML)                    │ SELECT (feed_v)
                                                           ▼
 Reviewer ◄──── Databricks App (FastAPI + template.html) ──┘
     │                       │
     └── Remove / Restore ──► INSERT into decisions (user_email, timestamp)
```

The job owns every table except `decisions`. The app reads `feed_v` and appends to `decisions`, and to
nothing else.

## 3. Unity Catalog objects

Schema: `<catalog>.lit_surveillance`

| Object | Type | Written by | Purpose |
|---|---|---|---|
| `articles` | table | job (MERGE on `pmid`) | Parsed PubMed record plus PMC full text, one row per PMID |
| `article_terms` | table | job | Which search terms matched which PMID |
| `articles_enriched` | table | job (overwritten each run) | Tags, priority, summary; recomputed every run so edits to terms or rules apply to old rows |
| `terms` | table | people (later: the app) | Replaces `terms.csv` |
| `exclusion_rules` | table | people (later: the app) | Replaces `exclusions.csv` |
| `decisions` | table | app (append only) | Reviewer Remove/Restore log; replaces `decisions.csv` |
| `runs` | table | job | One row per job run |
| `feed_v` | view | – | What the page shows: enriched rows plus the latest decision |
| `raw` | volume | job | PubMed and PMC XML as received |

### 3.1 DDL

```sql
CREATE SCHEMA IF NOT EXISTS <catalog>.lit_surveillance
  COMMENT 'PubMed literature surveillance for Medical Affairs';

CREATE VOLUME IF NOT EXISTS <catalog>.lit_surveillance.raw
  COMMENT 'PubMed efetch and PMC XML as received';

-- One row per PMID. Mirrors parse_pubmed_xml() + parse_pmc_xml() in surveillance.py.
CREATE TABLE IF NOT EXISTS <catalog>.lit_surveillance.articles (
  pmid              STRING NOT NULL,
  title             STRING,
  abstract          STRING,
  abstract_sections ARRAY<STRUCT<label: STRING, text: STRING>>,  -- structured abstract labels drive key points
  authors           ARRAY<STRING>,
  journal           STRING,
  journal_abbrev    STRING,
  pub_date          DATE,              -- NULL when PubMed gives no year
  doi               STRING,
  pmc               STRING,            -- 'PMC1234567'
  pub_types         ARRAY<STRING>,
  mesh              ARRAY<STRING>,
  grants            ARRAY<STRING>,
  coi               STRING,            -- conflict-of-interest statement
  issue             STRING,            -- 'Suppl' marks conference abstracts
  language          STRING,
  record_status     STRING,            -- MEDLINE / Publisher / In-Process ...
  url               STRING,
  pmc_abstract      STRING,
  pmc_body          STRING,            -- open-access full text, capped at 40,000 chars
  pmc_sections      ARRAY<STRUCT<title: STRING, text: STRING>>,
  raw_path          STRING,            -- volume file this version was parsed from
  first_seen        TIMESTAMP NOT NULL,
  last_fetched      TIMESTAMP,
  CONSTRAINT articles_pk PRIMARY KEY (pmid)
);

CREATE TABLE IF NOT EXISTS <catalog>.lit_surveillance.article_terms (
  pmid          STRING NOT NULL,
  term          STRING NOT NULL,
  first_matched TIMESTAMP NOT NULL,
  run_id        STRING,
  CONSTRAINT article_terms_pk PRIMARY KEY (pmid, term)
);

-- Output of enrich() + article_links(). Overwritten on every run.
CREATE TABLE IF NOT EXISTS <catalog>.lit_surveillance.articles_enriched (
  pmid         STRING NOT NULL,
  disease      ARRAY<STRING>,          -- Psoriasis, Atopic Dermatitis, Seb Derm, HS, Vitiligo, Other
  product      STRING,                 -- Roflumilast / Competitor / Class
  study_type   STRING,
  sponsor      STRING,
  access       STRING,                 -- Full text / Abstract / Title only
  rule         STRING,                 -- exclusion rule that fired, else NULL
  format_note  STRING,
  priority     STRING,                 -- High / Medium / Check / Low
  summary      STRING,
  key_points   ARRAY<STRING>,
  why          STRING,
  links        ARRAY<STRUCT<label: STRING, url: STRING>>,
  enriched_at  TIMESTAMP,
  run_id       STRING,
  CONSTRAINT articles_enriched_pk PRIMARY KEY (pmid)
);

CREATE TABLE IF NOT EXISTS <catalog>.lit_surveillance.terms (
  term             STRING NOT NULL,
  product_category STRING NOT NULL,    -- Roflumilast / Competitor / Class
  pubmed_query     STRING NOT NULL,
  active           BOOLEAN NOT NULL,
  updated_by       STRING,
  updated_at       TIMESTAMP,
  CONSTRAINT terms_pk PRIMARY KEY (term)
);

CREATE TABLE IF NOT EXISTS <catalog>.lit_surveillance.exclusion_rules (
  rule_name   STRING NOT NULL,
  pattern     STRING NOT NULL,         -- Python regex, e.g. (?i)\b(COPD|asthma)\b
  applies_to  STRING NOT NULL,         -- title / title_abstract
  note        STRING,
  active      BOOLEAN NOT NULL,
  updated_by  STRING,
  updated_at  TIMESTAMP,
  CONSTRAINT exclusion_rules_pk PRIMARY KEY (rule_name)
);

-- Append only. The latest row per pmid wins; history is the audit trail.
CREATE TABLE IF NOT EXISTS <catalog>.lit_surveillance.decisions (
  decision_id STRING NOT NULL,         -- uuid
  pmid        STRING NOT NULL,
  decision    STRING NOT NULL,         -- removed / restored
  reason      STRING,
  user_email  STRING NOT NULL,         -- from X-Forwarded-Email
  decided_at  TIMESTAMP NOT NULL,
  CONSTRAINT decisions_pk PRIMARY KEY (decision_id)
);

CREATE TABLE IF NOT EXISTS <catalog>.lit_surveillance.runs (
  run_id          STRING NOT NULL,     -- Databricks job run id
  started_at      TIMESTAMP NOT NULL,
  finished_at     TIMESTAMP,
  window_start    DATE,
  window_end      DATE,
  status          STRING,              -- running / ok / failed
  pmids_matched   INT,
  new_count       INT,
  refreshed_count INT,                 -- incomplete records re-checked (ahead-of-print, embargo)
  missing_pmids   ARRAY<STRING>,       -- listed by search but not yet returned by efetch
  error           STRING,
  CONSTRAINT runs_pk PRIMARY KEY (run_id)
);

-- What the page shows. Same status logic as to_page_row() in surveillance.py.
CREATE OR REPLACE VIEW <catalog>.lit_surveillance.feed_v AS
WITH latest AS (
  SELECT pmid, decision, user_email, decided_at
  FROM <catalog>.lit_surveillance.decisions
  QUALIFY ROW_NUMBER() OVER (PARTITION BY pmid ORDER BY decided_at DESC) = 1
),
terms AS (
  SELECT pmid, array_sort(collect_set(term)) AS matched_terms
  FROM <catalog>.lit_surveillance.article_terms GROUP BY pmid
)
SELECT a.pmid, a.title, a.authors, a.journal, a.journal_abbrev, a.pub_date, a.pmc, a.first_seen,
       t.matched_terms, e.* EXCEPT (pmid),
       CASE WHEN d.decision = 'restored' THEN 'in'
            WHEN d.decision = 'removed'  THEN 'out'
            WHEN e.rule IS NOT NULL      THEN 'out'
            ELSE 'in' END                                            AS status,
       CASE WHEN d.decision = 'restored' THEN NULL
            WHEN e.rule IS NOT NULL      THEN e.rule
            WHEN d.decision = 'removed'  THEN 'Removed by reviewer' END AS status_reason,
       d.user_email AS decided_by, d.decided_at
FROM <catalog>.lit_surveillance.articles a
JOIN <catalog>.lit_surveillance.articles_enriched e USING (pmid)
LEFT JOIN terms t USING (pmid)
LEFT JOIN latest d USING (pmid);
```

Primary keys in Unity Catalog are informational, not enforced. The job keeps them true by using `MERGE`.

### 3.2 Volume layout

```
/Volumes/<catalog>/lit_surveillance/raw/
  pubmed/<yyyy-mm-dd>/<run_id>/efetch_<batch>.xml
  pmc/<PMCID>.xml                       -- latest copy per PMC article
  snapshots/literature_update_<yyyy-mm-dd>.html   -- optional: static page per run, for sharing or archiving
```

### 3.3 Mapping from today's files

| Today | Target |
|---|---|
| `articles.json` fields `pmid … url`, `mesh`, `first_seen` | `articles` |
| `articles.json` `sections` | `articles.abstract_sections` |
| `articles.json` `pmc_text.{abstract, body, sections}` | `articles.pmc_abstract`, `pmc_body`, `pmc_sections` |
| `articles.json` `matched_terms` | `article_terms` (one row per term) |
| `articles.json` `disease … why`, `points` | `articles_enriched` (`points` → `key_points`) |
| `terms.csv` | `terms` (with `active = true`) |
| `exclusions.csv` | `exclusion_rules` (with `active = true`) |
| `decisions.csv` | `decisions` (with `user_email = 'migrated'`) |

A one-off backfill notebook loads the current `articles.json` and CSVs, so nothing already collected is lost.

## 4. Components

### 4.1 Job: `lit-surveillance-daily`

- Serverless job with one Python task, scheduled daily, e.g. 06:00 local time.
- Steps (same logic as `surveillance.py main()`):
  1. Read the active rows from `terms` and `exclusion_rules`.
  2. Run an esearch for each term over the date window, then efetch the new PMIDs plus incomplete records from the last 120 days.
  3. Write the raw XML to the volume.
  4. `MERGE` into `articles` and `article_terms`.
  5. Recompute `enrich()` for every article and overwrite `articles_enriched`.
  6. Write a row to `runs`.
- Reads the NCBI key with `dbutils.secrets.get("lit-surveillance", "pubmed-api-key")`.
- The parsing and tagging functions stay pure Python and move to a shared module, so `tests/test_offline.py` keeps working without a network.

### 4.2 App: `lit-surveillance`

| Route | Does |
|---|---|
| `GET /` | Serves `template.html`, which now loads its data from the API |
| `GET /api/articles?days=90` | Rows from `feed_v`, shaped exactly like `to_page_row()` output, so the page's JS is unchanged |
| `POST /api/decisions` | `{pmid, decision, reason}` → inserts into `decisions` with the viewer's email and the time |
| `GET /api/runs/latest` | Last run's window, counts and status, for the page header |
| later: `GET/PUT /api/terms`, `/api/rules` | Edit search terms and exclusion rules from the UI |

Page row contract (the fields the JS reads):
`id, title, cite, meta, src, via, term, dz, product, type, access, prio, sponsor, link, links, summary,
points, why, status, rule, note, pub_date`.

### 4.3 Repo layout (target)

```
app_ls/
  databricks.yml                 bundle: variables, targets (dev, prod)
  resources/
    app.yml                      app + its resources (warehouse, table grants)
    job.yml                      daily job + schedule
  src/lit/
    pubmed.py                    E-utilities client, XML parsing
    enrich.py                    tagging, priority, summaries, links
  jobs/run_surveillance.py       job entry point (Spark writes to UC)
  app/
    app.py                       FastAPI
    app.yaml
    requirements.txt
    static/index.html            today's template.html
  sql/ddl.sql                    section 3.1
  notebooks/backfill.py          one-off load of articles.json + CSVs
  tests/
  .github/workflows/deploy.yml   databricks bundle deploy -t prod on push to main
```

### 4.4 Bundle sketch

Check this against the current Databricks Asset Bundles docs before using it.

```yaml
bundle:
  name: lit-surveillance

variables:
  catalog: {default: "<catalog>"}
  warehouse_id: {description: "SQL warehouse for the app"}

resources:
  apps:
    lit_surveillance:
      name: lit-surveillance
      source_code_path: ./app
      resources:
        - name: sql-warehouse
          sql_warehouse: {id: "${var.warehouse_id}", permission: CAN_USE}

  jobs:
    surveillance_daily:
      name: lit-surveillance-daily
      schedule: {quartz_cron_expression: "0 0 6 * * ?", timezone_id: "America/Los_Angeles"}
      tasks:
        - task_key: fetch
          spark_python_task:
            python_file: ./jobs/run_surveillance.py
            parameters: ["--catalog", "${var.catalog}", "--days", "30"]
          environment_key: default
      environments:
        - environment_key: default
          spec: {client: "2", dependencies: ["requests"]}

targets:
  dev:  {mode: development, default: true}
  prod: {mode: production}
```

## 5. Permissions

| Principal | Needs |
|---|---|
| App service principal (created with the app) | `USE CATALOG`, `USE SCHEMA`; `SELECT` on `feed_v`, `runs`; `MODIFY` + `SELECT` on `decisions`; *Can use* on the SQL warehouse |
| Job identity (service principal in prod) | `USE CATALOG`, `USE SCHEMA`; `MODIFY` + `SELECT` on all tables; `READ VOLUME` + `WRITE VOLUME` on `raw`; *Read* on secret scope `lit-surveillance` |
| Reviewers (group) | *Can use* on the app. No direct table access needed |
| Analysts (optional group) | `SELECT` on the schema, for notebooks and dashboards |

```sql
GRANT USE CATALOG ON CATALOG <catalog> TO `<app-sp>`;
GRANT USE SCHEMA  ON SCHEMA  <catalog>.lit_surveillance TO `<app-sp>`;
GRANT SELECT ON TABLE <catalog>.lit_surveillance.feed_v    TO `<app-sp>`;
GRANT SELECT ON TABLE <catalog>.lit_surveillance.runs      TO `<app-sp>`;
GRANT SELECT, MODIFY ON TABLE <catalog>.lit_surveillance.decisions TO `<app-sp>`;
```

## 6. What to ask the Databricks admin

1. **Catalog and schema:** a catalog we can create `lit_surveillance` in, or an existing schema we can use.
2. **SQL warehouse:** a small serverless one (ID needed for the bundle).
3. **Databricks Apps:** enabled in the workspace.
4. **Internet access:** outbound access from serverless job compute to `eutils.ncbi.nlm.nih.gov` (and `pmc.ncbi.nlm.nih.gov`).
5. **Secret scope:** `lit-surveillance`, holding the NCBI key.
6. **Service principal for CI:** one for GitHub Actions deploys, with its token stored as a GitHub secret.
7. **GitHub connection:** if the org restricts Git servers, allow the company GitHub org.

## 7. Plan

| Phase | Work | Result |
|---|---|---|
| 0 (done) | Interim app (`app.py`) serving the flat-file page | Wireframe viewable in Databricks |
| 1 | Run the DDL; backfill `articles.json` and the CSVs | Data lives in Unity Catalog |
| 2 | Split `surveillance.py` into `src/lit/` + job entry point; schedule the job | Daily refresh, raw XML in volume |
| 3 | FastAPI app on `feed_v`; page loads from the API; decisions persisted with user and time | Reviewer choices stick and are audited |
| 4 | Bundle + GitHub Actions; move the repo out of Box (Box sync corrupts `.git`) | Push to `main` deploys |
| 5 (optional) | Edit terms/rules in the app; more sources (the template already has chips for OpenAlex, Europe PMC, Crossref, Google Scholar) | Wider coverage, no CSV editing |

## 8. Decisions deferred

- **Decision write latency:** each Delta insert through a warehouse takes about 1 s. If Remove/Restore feels slow, move `decisions` to **Lakebase** (managed Postgres), synced to Unity Catalog. Not needed at the start.
- **Model summaries:** summaries are extractive (the authors' own sentences), so nothing is invented. A Databricks-served model could replace `summarise()` later; for medical content that needs review before the page shows it.
- **Retention of `pmc_body`:** full text is stored for summarising. Check whether keeping open-access full text in a table is acceptable, or keep only the summary and a link.
