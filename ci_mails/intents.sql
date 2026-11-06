-- The L3 reasons (the intent list) and the articles from the INFORM "Articles of Interest" emails they were learned from,
-- plus a view of the surveillance articles ranked by L3 score. Prefix apps_prod.literature_surveillance, like
-- tables.sql. build_tables.py creates these and fills the two tables; running it again replaces their rows.
-- The daily job (surveillance.py) reads both tables: it scores with the active rows of _intents (the only copy of the
-- L3 reasons), and puts every article in _ci_articles on the accepted feed.

CREATE TABLE IF NOT EXISTS apps_prod.literature_surveillance.intents (
  intent_id        STRING NOT NULL COMMENT 'Short id, e.g. own_product',
  kind             STRING NOT NULL COMMENT 'intent: an L3 reason, adds its weight when it matches; deprioritise: a push-down rule, takes points off',
  intent           STRING NOT NULL COMMENT 'Name shown to reviewers',
  rank             INT             COMMENT 'Order: the L3 reasons, most important first, then the push-down rules; ties in weight keep this order',
  weight           INT             COMMENT 'intent: the score when it is the best match; deprioritise: the points taken off (negative)',
  why              STRING          COMMENT 'Why Medical Affairs sends (or skips) such articles, from the INFORM emails',
  keyword_groups   ARRAY<STRUCT<field: STRING, keywords: ARRAY<STRING>>>
                                   COMMENT 'intent: every group needs one of its L3 keywords in its field (title, title_abstract, or full_text: title + abstract + the PMC body, when the article has one). Keywords match like L3 priority keywords: case and punctuation ignored, each word a prefix',
  none_of          ARRAY<STRING>   COMMENT 'intent: does not match when one of these is in the title',
  title_regex      STRING          COMMENT 'deprioritise: fires when the title matches (case-insensitive Python regex)',
  text_regex       STRING          COMMENT 'deprioritise: fires when the abstract, conflict-of-interest statement or funders match',
  study_types      ARRAY<STRING>   COMMENT 'deprioritise: fires for these study types',
  unless_regex     STRING          COMMENT 'deprioritise: does not fire when the title matches this',
  l3_keywords      ARRAY<STRING>   COMMENT 'Every keyword of the intent, flat',
  flagged_matched  INT             COMMENT 'Backtest: emailed articles it matches (of 194)',
  flagged_main     INT             COMMENT 'Backtest: of those, ones summarised in the email body (main tier)',
  pool_matched     INT             COMMENT 'Backtest: articles the L1 search found Jan-Oct 2026 that it matches',
  pool_precision   DOUBLE          COMMENT 'Backtest: share of pool_matched that were emailed',
  examples         ARRAY<STRING>   COMMENT 'Titles of emailed articles it matches (up to 5)',
  active           BOOLEAN NOT NULL COMMENT 'false switches the row off without deleting it',
  updated_at       TIMESTAMP,
  updated_by       STRING
)
COMMENT 'Literature surveillance: the L3 reasons (intent list) the daily job scores articles with. Why Medical Affairs flags an article, learned from the INFORM Articles of Interest emails (Jan-Oct 2026), as keyword groups with a weight, plus push-down rules. Changes apply from the next run.';

CREATE TABLE IF NOT EXISTS apps_prod.literature_surveillance.ci_articles (
  email_item     INT NOT NULL    COMMENT 'Order across all emails, oldest first',
  email_subject  STRING,
  email_date     TIMESTAMP,
  email_pdf      STRING          COMMENT 'The email as PDF in the ci_mails volume',
  tier           STRING          COMMENT 'main: summarised in the body of the email; fyi: listed under A few more articles FYI, not ordered, case reports',
  section        STRING          COMMENT 'Heading the article was listed under, if any',
  citation       STRING          COMMENT 'Citation as written in the email',
  title          STRING          COMMENT 'Title from PubMed, else OpenAlex, else the email',
  journal        STRING,
  pub_date       DATE,
  pmid           STRING,
  doi            STRING,
  openalex_id    STRING,
  in_pubmed      BOOLEAN,
  in_openalex    BOOLEAN,
  found_in       STRING          COMMENT 'PubMed and OpenAlex, PubMed only, OpenAlex only, or Neither (publisher or Google Scholar only)',
  links          ARRAY<STRING>   COMMENT 'Links in the email under the citation (article, ReadCube, supplement)',
  reviewer_note  STRING          COMMENT 'What the email says about the article',
  in_l1_pool     BOOLEAN         COMMENT 'The current L1 terms find it in PubMed or OpenAlex',
  l1_terms       ARRAY<STRING>   COMMENT 'L1 terms that find it',
  article_id     STRING          COMMENT 'articles.id: of the article the daily job has, else of the one it adds from record',
  intents        ARRAY<STRING>   COMMENT 'L3 reasons its title and abstract match, best first',
  intent_score   INT             COMMENT 'L3 score when loaded, without L3 keywords from Settings',
  intent_band    STRING          COMMENT 'Must read (85+), Of interest (60-84), FYI (40-59), Low',
  important      BOOLEAN         COMMENT 'Flagged as important: true for every article sent in an INFORM email',
  record         STRING          COMMENT 'The article as the daily job stores it (JSON): added to its store, with the source Email, when its search has not found it',
  loaded_at      TIMESTAMP
)
COMMENT 'Literature surveillance: the articles Medical Affairs sent in the INFORM Articles of Interest emails (/Volumes/medical_prod/apps/ci_mails), matched to PubMed and OpenAlex. The daily job puts each on the accepted feed; the L3 reasons were learned from them.';

CREATE OR REPLACE VIEW apps_prod.literature_surveillance.ranked
COMMENT 'Literature surveillance articles ranked by L3 score, as the last run of the daily job scored them, with the feed each is on (a reviewer decision, else accepted automatically, else ignored by an exclusion rule, else review).'
AS
WITH a AS (
  SELECT id, aliases, title, journal, pub_date, first_seen, sources, matched_terms, study_type, priority, exclusion_rule, url,
         CAST(get_json_object(record, '$.l3_score') AS INT) AS l3_score,
         from_json(get_json_object(record, '$.l3_intents'), 'ARRAY<STRUCT<intent: STRING, points: INT>>') AS l3_reasons,
         from_json(get_json_object(record, '$.l3_push_down'), 'ARRAY<STRUCT<rule: STRING, points: INT>>') AS l3_push_down,
         get_json_object(record, '$.auto_accept') AS auto_accept
  FROM apps_prod.literature_surveillance.articles
)
SELECT a.id, a.title, a.journal, a.pub_date, a.first_seen, a.sources, a.matched_terms, a.study_type, a.priority, a.l3_score,
       transform(a.l3_reasons, x -> x.intent) AS l3_reasons, transform(a.l3_push_down, x -> x.rule) AS l3_push_down,
       a.auto_accept, a.exclusion_rule, d.decision, d.reasons,
       CASE WHEN d.decision = 'accepted' THEN 'accepted' WHEN d.decision IN ('ignored', 'removed') THEN 'ignored'
            WHEN d.decision = 'restored' THEN 'review' WHEN a.auto_accept IS NOT NULL THEN 'accepted'
            WHEN a.exclusion_rule IS NOT NULL THEN 'ignored' ELSE 'review' END AS feed,
       row_number() OVER (ORDER BY a.l3_score DESC NULLS LAST, a.pub_date DESC, a.id) AS l3_rank, a.url
FROM a LEFT JOIN apps_prod.literature_surveillance.decisions d
  ON d.article_id = a.id OR array_contains(a.aliases, d.article_id);
