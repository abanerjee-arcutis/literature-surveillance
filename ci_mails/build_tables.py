"""Fill literature_surveillance_intents and _ci_articles and create the _ranked view (intents.sql).

    python build_tables.py [--dry-run]

_intents gets the L3 reasons and push-down rules of surveillance.py, each with its backtest evidence; from the next
run the daily job scores with the table instead of its built-in list, so a row can be changed or switched off
there. _ci_articles gets every article sent in an Articles of Interest email, with the record the daily job adds to
its store when its own search has not found the article: the job puts all of them on the accepted feed.
--dry-run writes the rows to work/tables_payload.json and nothing to Databricks. Without it the two tables' rows are
replaced.
"""
import json
import os
import re
import sys
from datetime import datetime, timezone

from common import HERE, WORK, funding_text, load, study, sv
from dbsql import sql

PREFIX = "medical_prod.apps.literature_surveillance"
VOLUME = "/Volumes/medical_prod/apps/ci_mails"
WINDOW = ("2025-12-20", "2026-10-10")                  # as in backtest.py
NOW = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
INTENT_SCHEMA = ("ARRAY<STRUCT<intent_id: STRING, kind: STRING, intent: STRING, rank: INT, weight: INT, why: STRING, "
                 "keyword_groups: ARRAY<STRUCT<field: STRING, keywords: ARRAY<STRING>>>, none_of: ARRAY<STRING>, "
                 "title_regex: STRING, text_regex: STRING, study_types: ARRAY<STRING>, unless_regex: STRING, "
                 "l3_keywords: ARRAY<STRING>, flagged_matched: INT, flagged_main: INT, pool_matched: INT, pool_precision: DOUBLE, "
                 "examples: ARRAY<STRING>, active: BOOLEAN, updated_at: STRING, updated_by: STRING>>")
CI_SCHEMA = ("ARRAY<STRUCT<email_item: INT, email_subject: STRING, email_date: STRING, email_pdf: STRING, tier: STRING, "
             "section: STRING, citation: STRING, title: STRING, journal: STRING, pub_date: STRING, pmid: STRING, doi: STRING, "
             "openalex_id: STRING, in_pubmed: BOOLEAN, in_openalex: BOOLEAN, found_in: STRING, links: ARRAY<STRING>, "
             "reviewer_note: STRING, in_l1_pool: BOOLEAN, l1_terms: ARRAY<STRING>, article_id: STRING, intents: ARRAY<STRING>, "
             "intent_score: INT, intent_band: STRING, important: BOOLEAN, record: STRING, loaded_at: STRING>>")


def batches(rows, limit=900_000):
    """The rows as JSON arrays, each under the SQL Statement Execution API's limit of 1,048,576 characters for the
    parameters of one statement (the emailed articles' records, abstracts included, come to more than that)."""
    part, size = [], 2
    for r in rows:
        n = len(json.dumps(r)) + 2
        if part and size + n > limit:
            yield json.dumps(part)
            part, size = [], 2
        part.append(r)
        size += n
    if part:
        yield json.dumps(part)


def score(r):
    """The daily job's L3 score of a record, without L3 keywords from Settings: (score, reasons matched, push-downs)."""
    return sv.l3_score(sv.DEFAULT_L3_RULES, r["title"], r.get("abstract"), study(r), funding_text(r))


def store_record(a) -> dict:
    """The emailed article as the daily job stores it: PubMed's record, else OpenAlex's, else one made from the email's
    citation (publisher pages, UpToDate, news). Its source is 'Email' and it was first seen the day it was emailed."""
    pm, oa = a.get("pubmed"), a.get("openalex")
    if pm or oa:
        r = dict(pm or oa)
        for f in ("openalex_id", "oa_url"):
            r[f] = r.get(f) or (oa or {}).get(f)
    else:
        title, cite = a["rec"]["title"], a["citation"]
        at = cite.find(title)
        before, after = (cite[:at], cite[at + len(title):]) if at >= 0 else ("", "")
        names = [f"{last} {initials}" for last, initials in re.findall(r"([A-Z][A-Za-z'’-]+),?\s+([A-Z]{1,3})\b", before)]
        years = re.findall(r"\b20\d\d\b", cite)
        links = [u for u in a.get("links") or a.get("urls") or [] if "readcube" not in u]
        r = dict(origin="Email", pmid=None, title=title, abstract=None, sections=[],
                 authors=names[:1] + ["et al."] if "et al" in before else names,
                 journal=re.sub(r"[\s.,]*\b20\d\d\.?$", "", after.strip(" .")).strip(" ."), journal_abbrev="",
                 pub_date=f"{years[-1]}-01-01" if years else "", doi=None, pmc=None, pub_types=[], grants=[], coi="",
                 issue="", language="eng", record_status="", url=links[0] if links else "", mesh=[])
    r.update(sources=["Email"], matched_terms=[], first_seen=a["email_date"].rstrip("Z")[:19])
    return r


def ci_rows(emails, pool):
    """One row per emailed article, linked to the daily job's article: the one it has, else the one it will add."""
    prod = sql(f"SELECT id, pmid, doi, title FROM {PREFIX}_articles")
    p_pmid = {r["pmid"]: r["id"] for r in prod if r["pmid"]}
    p_doi = {sv.norm_doi(r["doi"]): r["id"] for r in prod if r["doi"]}
    p_title = {sv.title_key(r["title"]): r["id"] for r in prod}
    rows = []
    for a in emails:
        r, pm, oa = a["rec"], a.get("pubmed") or {}, a.get("openalex") or {}
        s, m, _ = score(r)
        found = ("PubMed and OpenAlex" if a["in_pubmed"] and a["in_openalex"] else "PubMed only" if a["in_pubmed"]
                 else "OpenAlex only" if a["in_openalex"] else "Neither")
        hit = pool.get(a.get("pool_key")) or {}
        doi = sv.norm_doi(pm.get("doi") or oa.get("doi") or (a.get("dois") or [None])[0])
        rec = store_record(a)
        rows.append(dict(
            email_item=a["email_item"], email_subject=a["email"], email_date=a["email_date"].replace("T", " ").rstrip("Z"),
            email_pdf=f"{VOLUME}/{a['pdf']}", tier=a["tier"], section=a["section"] or None, citation=a["citation"],
            title=r["title"], journal=r.get("journal") or None, pub_date=r.get("pub_date") or None,
            pmid=pm.get("pmid") or oa.get("pmid"), doi=doi, openalex_id=oa.get("openalex_id"),
            in_pubmed=a["in_pubmed"], in_openalex=a["in_openalex"],
            found_in=f"Not a journal article ({a['not_literature']})" if a.get("not_literature") else found,
            links=a.get("links") or a.get("urls") or [], reviewer_note=a["comment"] or None,
            in_l1_pool=bool(hit), l1_terms=hit.get("matched_terms") or a["l1_terms_text"],
            article_id=(p_pmid.get(pm.get("pmid")) or p_doi.get(doi) or p_title.get(sv.title_key(r["title"]))
                        or sv.record_key(rec)),
            intents=[h["intent"] for h in m], intent_score=s, intent_band=sv.l3_band(s), important=True,
            record=json.dumps(rec, ensure_ascii=False), loaded_at=NOW))
    return rows


def intent_rows(emails, pool, flagged):
    """The L3 reasons and push-down rules, each with its backtest evidence: emailed articles it matches and its
    precision among the articles the L1 terms found."""
    lit = [a for a in emails if not a.get("not_literature")]
    scored = [(k, r) for k, r in pool.items() if k in flagged or not r.get("pub_date") or WINDOW[0] <= r["pub_date"] <= WINDOW[1]]
    def ids(r):
        _, hits, pushed = score(r)
        return {x["id"] for x in hits + pushed}
    lit_ids = [(a, ids(a["rec"])) for a in lit]
    pool_ids = [(k, ids(r)) for k, r in scored]
    rows = []
    for n, rule in enumerate(sv.DEFAULT_L3_RULES, 1):
        hit = [a for a, xs in lit_ids if rule["intent_id"] in xs]
        in_pool = [k for k, xs in pool_ids if rule["intent_id"] in xs]
        rows.append(dict(
            rule, rank=n, l3_keywords=list(dict.fromkeys(k for g in rule["keyword_groups"] or [] for k in g["keywords"])) or None,
            flagged_matched=len(hit), flagged_main=sum(a["tier"] == "main" for a in hit), pool_matched=len(in_pool),
            pool_precision=round(sum(k in flagged for k in in_pool) / len(in_pool), 3) if in_pool else None,
            examples=[a["rec"]["title"] for a in sorted(hit, key=lambda a: a["tier"] != "main")[:5]],
            active=True, updated_at=NOW, updated_by="ci_mails/build_tables.py"))
    return rows


if __name__ == "__main__":
    emails, pool, flagged = load()
    intents, ci = intent_rows(emails, pool, flagged), ci_rows(emails, pool)
    json.dump(dict(intents=intents, ci=ci), open(os.path.join(WORK, "tables_payload.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print(f"{len(ci)} emailed articles; {sum(r['kind'] == 'intent' for r in intents)} L3 reasons, "
          f"{sum(r['kind'] != 'intent' for r in intents)} push-down rules")
    if "--dry-run" in sys.argv:
        sys.exit()
    ddl = re.sub(r"^--.*$", "", open(os.path.join(HERE, "intents.sql"), encoding="utf-8").read(), flags=re.M)
    for stmt in [s.strip() for s in re.split(r";[ \t]*$", ddl, flags=re.M) if s.strip()]:   # statements end a line with ';'
        sql(stmt)
    for i, part in enumerate(batches(intents)):     # the first batch replaces the table's rows, the others add to them
        sql(f"""INSERT {'INTO' if i else 'OVERWRITE'} {PREFIX}_intents
                SELECT intent_id, kind, intent, rank, weight, why, keyword_groups, none_of, title_regex, text_regex, study_types,
                       unless_regex, l3_keywords, flagged_matched, flagged_main, pool_matched, pool_precision, examples, active,
                       CAST(updated_at AS TIMESTAMP), updated_by
                FROM (SELECT inline(from_json(:rows, '{INTENT_SCHEMA}')))""", dict(rows=part))
    for i, part in enumerate(batches(ci)):
        sql(f"""INSERT {'INTO' if i else 'OVERWRITE'} {PREFIX}_ci_articles
                SELECT email_item, email_subject, CAST(email_date AS TIMESTAMP), email_pdf, tier, section, citation, title, journal,
                       try_cast(pub_date AS DATE), pmid, doi, openalex_id, in_pubmed, in_openalex, found_in, links, reviewer_note,
                       in_l1_pool, l1_terms, article_id, intents, intent_score, intent_band, important, record,
                       CAST(loaded_at AS TIMESTAMP)
                FROM (SELECT inline(from_json(:rows, '{CI_SCHEMA}')))""", dict(rows=part))
    print(sql(f"SELECT (SELECT count(*) FROM {PREFIX}_intents) intents, (SELECT count(*) FROM {PREFIX}_ci_articles) ci")[0])
