-- Back-end tables for the literature surveillance job and app, named <prefix>_articles, _runs, _decisions, _settings
-- (prefix medical_prod.apps.literature_surveillance). Run once in the SQL editor (DEPLOY.md, Step 3); running it again changes nothing.
-- The daily job writes _articles and _runs; the app writes _decisions and _settings, and builds the page from all four.
-- The column lists must match article_row(), decision_row(), settings_row() and run_row() in surveillance.py
-- (tests/test_offline.py checks).

CREATE TABLE IF NOT EXISTS medical_prod.apps.literature_surveillance_articles (
  id             STRING NOT NULL COMMENT 'Article id shown on the page: the PMID, else doi:<doi>, openalex:<id> or scholar:<id>',
  aliases        ARRAY<STRING>   COMMENT 'Earlier ids of the same article (e.g. a Google Scholar hit later found in PubMed); a decision may be stored under one',
  pmid           STRING,
  doi            STRING,
  pmc            STRING          COMMENT 'PubMed Central id, when PMC has the article',
  title          STRING,
  authors        ARRAY<STRING>,
  journal        STRING,
  pub_date       DATE,
  first_seen     TIMESTAMP       COMMENT 'When a run first found the article',
  sources        ARRAY<STRING>   COMMENT 'PubMed, OpenAlex and/or Google Scholar',
  matched_terms  ARRAY<STRING>   COMMENT 'L1 search terms whose query returned the article',
  disease        ARRAY<STRING>,
  product        STRING          COMMENT 'Roflumilast, Competitor or Class',
  study_type     STRING,
  intent         STRING          COMMENT 'L2 intent: Safety, Efficacy, Real-world use, Mechanism, Disease state, Patient perspective, Review & guidance or Other',
  access         STRING          COMMENT 'Full text, Abstract or Title only',
  priority       STRING          COMMENT 'High, Medium, Check or Low',
  l3_keywords    ARRAY<STRING>   COMMENT 'L3 priority keywords in the title or abstract',
  exclusion_rule STRING          COMMENT 'Exclusion rule that puts the article on the Ignored feed unless a reviewer decided otherwise',
  sponsor        STRING,
  abstract       STRING,
  summary        STRING,
  url            STRING,
  record         STRING NOT NULL COMMENT 'The whole record as JSON, PMC full text included; the job reads the store back from this column',
  page_position  INT             COMMENT 'Place on the page as of the last run, 0 first; NULL when older than the page shows',
  page_row       STRING          COMMENT 'The article as the page shows it (JSON, without the reviewer decision, which the app adds); NULL when not on the page'
)
COMMENT 'Literature surveillance: one row per article found. The daily job rewrites the table on every run and is its only writer.';

CREATE TABLE IF NOT EXISTS medical_prod.apps.literature_surveillance_decisions (
  article_id STRING NOT NULL COMMENT 'literature_surveillance_articles.id, or one of its aliases',
  decision   STRING NOT NULL COMMENT 'accepted, ignored, or restored (back to review despite an exclusion rule)',
  reasons    ARRAY<STRING>   COMMENT 'Keywords and reasons the reviewer ticked when accepting or ignoring',
  decided_by STRING          COMMENT 'Reviewer email',
  decided_at TIMESTAMP
)
COMMENT 'Literature surveillance: reviewer decisions saved by the app, one row per article that has one. Undo deletes the row.'
TBLPROPERTIES ('delta.enableDeletionVectors' = 'true', 'delta.enableRowTracking' = 'true');  -- two reviewers saving at once don't conflict

CREATE TABLE IF NOT EXISTS medical_prod.apps.literature_surveillance_settings (
  updated_at        TIMESTAMP NOT NULL,
  updated_by        STRING,
  sources           ARRAY<STRING> COMMENT 'Sources switched on: pubmed, openalex, scholar',
  scholar_pages     INT           COMMENT 'Google Scholar pages of 20 results per term',
  priority_keywords ARRAY<STRING> COMMENT 'L3 priority keywords',
  terms             ARRAY<STRUCT<term: STRING, product_category: STRING, pubmed_query: STRING, openalex_query: STRING, scholar_query: STRING>>
                                  COMMENT 'L1 search terms; empty openalex_query / scholar_query means derived from pubmed_query'
)
COMMENT 'Literature surveillance: the Settings dialog, one row per Save in the app. The newest row applies; when there is none, the job saves terms.csv as the first.';

CREATE TABLE IF NOT EXISTS medical_prod.apps.literature_surveillance_runs (
  run_at            TIMESTAMP NOT NULL,
  window_start      DATE          COMMENT 'Searched for articles from this date',
  window_end        DATE          COMMENT 'to this date',
  sources           ARRAY<STRING> COMMENT 'Sources searched: pubmed, openalex, scholar',
  scholar_pages     INT,
  priority_keywords ARRAY<STRING> COMMENT 'L3 priority keywords',
  terms             ARRAY<STRUCT<term: STRING, product_category: STRING, pubmed_query: STRING, openalex_query: STRING, scholar_query: STRING>>
                                  COMMENT 'L1 search terms searched',
  terms_from        STRING        COMMENT 'Where the terms came from: the settings table, or a CSV given with --terms',
  api_keys          STRUCT<pubmed: BOOLEAN, openalex: BOOLEAN, scholar: BOOLEAN> COMMENT 'Which API keys the run had',
  articles          INT           COMMENT 'Articles in the store after the run',
  new_articles      INT           COMMENT 'Articles this run found for the first time',
  shown             INT           COMMENT 'Articles on the page'
)
COMMENT 'Literature surveillance: one row per run of the daily job. The app takes the page header and what the last run searched with from the newest row.';
