-- Back-end tables for the literature surveillance job and app, named <prefix>_articles, _runs, _decisions, _settings
-- (prefix apps_prod.literature_surveillance). Run once in the SQL editor (DEPLOY.md, Step 3); running it again changes nothing.
-- The daily job writes _articles and _runs; the app writes _decisions and _settings, and builds the page from all four.
-- _rules holds the rules that tag and filter articles and the lists the page offers; the L3 reasons are in _intents (ci_mails/intents.sql).
-- Nothing is built into the code: the job and the app read these tables.
-- The column lists must match article_row(), decision_row(), settings_row() and run_row() in surveillance.py
-- (tests/test_offline.py checks).

CREATE TABLE IF NOT EXISTS apps_prod.literature_surveillance.articles (
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
  priority       STRING          COMMENT 'From the L3 score: Must read (85+), Of interest (60-84), FYI (40-59) or Low; Low when an exclusion rule fired',
  l3_keywords    ARRAY<STRING>   COMMENT 'L3 priority keywords saved in Settings that are in the title or abstract (the L3 reasons matched, the score and the push-down rules are in record)',
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

CREATE TABLE IF NOT EXISTS apps_prod.literature_surveillance.decisions (
  article_id STRING NOT NULL COMMENT 'articles.id, or one of its aliases',
  decision   STRING NOT NULL COMMENT 'accepted, ignored, or restored (back to review despite an exclusion rule, a Must read score or an Articles of Interest email)',
  reasons    ARRAY<STRING>   COMMENT 'Keywords and reasons the reviewer ticked when accepting or ignoring',
  decided_by STRING          COMMENT 'Reviewer email',
  decided_at TIMESTAMP
)
COMMENT 'Literature surveillance: reviewer decisions saved by the app, one row per article that has one. Undo deletes the row.'
TBLPROPERTIES ('delta.enableDeletionVectors' = 'true', 'delta.enableRowTracking' = 'true');  -- two reviewers saving at once don't conflict

CREATE TABLE IF NOT EXISTS apps_prod.literature_surveillance.bookmarks (
  article_id    STRING NOT NULL COMMENT 'articles.id, or one of its aliases',
  user_email    STRING NOT NULL COMMENT 'Viewer who bookmarked it; bookmarks are private to each viewer',
  bookmarked_at TIMESTAMP
)
COMMENT 'Literature surveillance: articles bookmarked in the app, one row per viewer and article. Removing a bookmark deletes the row.'
TBLPROPERTIES ('delta.enableDeletionVectors' = 'true', 'delta.enableRowTracking' = 'true');

CREATE TABLE IF NOT EXISTS apps_prod.literature_surveillance.settings (
  updated_at        TIMESTAMP NOT NULL,
  updated_by        STRING,
  sources           ARRAY<STRING> COMMENT 'Sources switched on: pubmed, openalex, scholar',
  scholar_pages     INT           COMMENT 'Google Scholar pages of 20 results per term',
  priority_keywords ARRAY<STRING> COMMENT 'L3 priority keywords',
  terms             ARRAY<STRUCT<term: STRING, product_category: STRING, pubmed_query: STRING, openalex_query: STRING, scholar_query: STRING>>
                                  COMMENT 'L1 search terms; empty openalex_query / scholar_query means derived from pubmed_query'
)
COMMENT 'Literature surveillance: the Settings dialog, one row per Save in the app. The newest row applies; the job stops when there is none.';

CREATE TABLE IF NOT EXISTS apps_prod.literature_surveillance.runs (
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
  shown             INT           COMMENT 'Articles on the page',
  l3_rules          STRING        COMMENT 'The L3 reasons and push-down rules the run scored with, as JSON (the _intents table)'
)
COMMENT 'Literature surveillance: one row per run of the daily job. The app takes the page header and what the last run searched with from the newest row.';

-- Tables created before the L3 score (November 2026) lack the runs table's last column. Add it once; until then the job
-- skips it and the page's Settings → L3 tab cannot list the L3 reasons:
--   ALTER TABLE apps_prod.literature_surveillance.runs ADD COLUMNS (l3_rules STRING COMMENT 'The L3 reasons and push-down rules the run scored with, as JSON (the _intents table)');

CREATE TABLE IF NOT EXISTS apps_prod.literature_surveillance.rules (
  kind          STRING NOT NULL COMMENT 'What the row is: exclusion, disease, study_type, study_type_text, openalex_type, intent_label, intent_rule, intent_abstract_rule, keyword, sponsor_company, reason_accepted, reason_ignored, l3_band, l3_param, default, not_article_type, not_article_title, ci_fix, ci_window (see DEPLOY.md, Step 3)',
  name          STRING NOT NULL COMMENT 'The label the rule gives, or the rule name',
  rank          INT             COMMENT 'Order within its kind; where the first match wins, the lowest rank is tried first',
  pattern       STRING          COMMENT 'Python regex (case-insensitive except exclusion rows, which carry their own (?i) flag); for default and ci_fix rows a plain value',
  unless_pattern STRING         COMMENT 'intent_rule: does not fire when the title matches this',
  match_values  ARRAY<STRING>   COMMENT 'study_type: the PubMed publication type; openalex_type: the OpenAlex work type; intent_rule: the study types that fire it',
  applies_to    STRING          COMMENT 'exclusion: title or title_abstract; intent_rule: study_type_or_title (default) or study_type_and_title',
  weight        INT             COMMENT 'l3_band: lowest L3 score in the band; l3_param: the value',
  note          STRING          COMMENT 'Why the rule exists; for intent_rule rows that need both a study type and a title match, the reason shown ({study_type} is filled in)',
  active        BOOLEAN NOT NULL COMMENT 'false switches the row off without deleting it',
  updated_at    TIMESTAMP,
  updated_by    STRING
)
COMMENT 'Literature surveillance: the rules the daily job tags and filters articles with (exclusion rules, disease / study type / intent / keyword tagging, sponsors) and the lists the page offers (reviewer reasons, priority bands). Changes apply from the next run; the app reads the lists on every request.';
