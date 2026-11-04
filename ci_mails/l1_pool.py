"""Everything the L1 terms find in PubMed and OpenAlex over the months the emails cover.

    python l1_pool.py [since] [until]          (default 2025-12-15 to 2026-10-16; writes work/l1_pool.json)

This is the pool the reviewer chose from: the pool articles the emails sent are the positives, the rest negatives.
Searched like the daily job (surveillance.py): PubMed by entry date, OpenAlex by publication date keeping only
title/abstract matches, OpenAlex hits that carry a PMID replaced by the PubMed record.
"""
import json
import os
import sys
from datetime import date

import requests

from common import TERMS, WORK, clients, sv

since = date.fromisoformat(sys.argv[1] if len(sys.argv) > 1 else "2025-12-15")
until = date.fromisoformat(sys.argv[2] if len(sys.argv) > 2 else "2026-10-16")
pm, oa = clients()


def fetch(pmids):
    """PubMed records; a batch PubMed refuses is retried one PMID at a time, skipping the bad ones."""
    out = []
    for i in range(0, len(pmids), 100):
        try:
            out += pm.fetch(pmids[i:i + 100])
        except requests.HTTPError:
            for p in pmids[i:i + 100]:
                try:
                    out += pm.fetch([p])
                except requests.HTTPError:
                    print("  skipped PMID", p, flush=True)
    return out


store, hits = {}, {}
for t in TERMS:
    ids = pm.search(t["pubmed_query"], since, until, retmax=2000)
    print(f"PubMed   {len(ids):5d}  {t['term']}", flush=True)
    for p in ids:
        hits.setdefault(p, set()).add(t["term"])
for r in fetch(sorted(hits)):
    r["matched_terms"], r["sources"] = sorted(hits.get(r["pmid"], ())), ["PubMed"]
    store[f"pmid:{r['pmid']}"] = r
by_doi = {sv.norm_doi(r.get("doi")): k for k, r in store.items() if r.get("doi")}
by_title = {sv.title_key(r["title"]): k for k, r in store.items()}
need_pmid = []
for t in TERMS:
    q = t["openalex_query"] or sv.plain_query(t["pubmed_query"])
    works = oa.search(q, since, until, max_pages=5)
    kept = [w for w in works if sv.query_matches(q, f"{w['title']} {w.get('abstract') or ''}")]
    print(f"OpenAlex {len(kept):5d}/{len(works):5d}  {t['term']}", flush=True)
    for w in kept:
        k = (f"pmid:{w['pmid']}" if w.get("pmid") and f"pmid:{w['pmid']}" in store else
             by_doi.get(sv.norm_doi(w.get("doi"))) or by_title.get(sv.title_key(w["title"])))
        if k:
            store[k]["matched_terms"] = sorted(set(store[k]["matched_terms"]) | {t["term"]})
            if "OpenAlex" not in store[k]["sources"]:
                store[k]["sources"].append("OpenAlex")
            store[k].setdefault("openalex_id", w.get("openalex_id"))
            continue
        k = f"pmid:{w['pmid']}" if w.get("pmid") else f"doi:{w['doi']}" if w.get("doi") else f"openalex:{w['openalex_id']}"
        w["matched_terms"], w["sources"] = [t["term"]], ["OpenAlex"]
        store[k] = w
        if w.get("doi"):
            by_doi[sv.norm_doi(w["doi"])] = k
        by_title[sv.title_key(w["title"])] = k
        if w.get("pmid"):
            need_pmid.append(k)
fetched = {r["pmid"]: r for r in fetch([store[k]["pmid"] for k in need_pmid])}
for k in need_pmid:                                    # PubMed's record has the abstract, publication types and MeSH
    r = fetched.get(store[k]["pmid"])
    if r:
        r["matched_terms"], r["sources"], r["openalex_id"] = store[k]["matched_terms"], ["OpenAlex"], store[k].get("openalex_id")
        store[k] = r
os.makedirs(WORK, exist_ok=True)
json.dump(store, open(os.path.join(WORK, "l1_pool.json"), "w", encoding="utf-8"), ensure_ascii=False)
print(len(store), "articles")
