-- One-off: copy the literature surveillance tables from medical_prod.apps to apps_prod.literature_surveillance
-- (prefix dropped: literature_surveillance_articles -> articles). The originals are left in place.
-- Needs CREATE SCHEMA + USE CATALOG on apps_prod. Compare the row counts afterwards.

CREATE SCHEMA IF NOT EXISTS apps_prod.literature_surveillance;

CREATE TABLE IF NOT EXISTS apps_prod.literature_surveillance.articles    DEEP CLONE medical_prod.apps.literature_surveillance_articles;
CREATE TABLE IF NOT EXISTS apps_prod.literature_surveillance.bookmarks   DEEP CLONE medical_prod.apps.literature_surveillance_bookmarks;
CREATE TABLE IF NOT EXISTS apps_prod.literature_surveillance.ci_articles DEEP CLONE medical_prod.apps.literature_surveillance_ci_articles;
CREATE TABLE IF NOT EXISTS apps_prod.literature_surveillance.decisions   DEEP CLONE medical_prod.apps.literature_surveillance_decisions;
CREATE TABLE IF NOT EXISTS apps_prod.literature_surveillance.intents     DEEP CLONE medical_prod.apps.literature_surveillance_intents;
CREATE TABLE IF NOT EXISTS apps_prod.literature_surveillance.rules       DEEP CLONE medical_prod.apps.literature_surveillance_rules;
CREATE TABLE IF NOT EXISTS apps_prod.literature_surveillance.runs        DEEP CLONE medical_prod.apps.literature_surveillance_runs;
CREATE TABLE IF NOT EXISTS apps_prod.literature_surveillance.settings    DEEP CLONE medical_prod.apps.literature_surveillance_settings;

-- then re-run the CREATE OR REPLACE VIEW in ci_mails/intents.sql against apps_prod.literature_surveillance.ranked
-- Expected counts: articles 1098, bookmarks 0, ci_articles 196, decisions 4, intents 32, rules 156, runs 18, settings 2
