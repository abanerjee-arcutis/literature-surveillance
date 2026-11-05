"""Find each emailed article in PubMed and OpenAlex.

    python resolve.py          (reads work/email_articles.json, writes work/email_resolved.json)

PubMed: the PMID in the email link, else a PMID found by DOI, else by title search. OpenAlex: by DOI, else PMID,
else title search. A hit counts only when its title matches the email's title, so a wrong link in the email or a
loose search result is not taken for the article. Articles still missing get a second, looser search (the first
eight distinctive title words). Progress is cached in work/email_resolved.cache.jsonl, so a rerun resumes. Hand corrections of a parsed citation
(a wrong DOI, a missing title) are the ci_fix rows of the _rules table.
"""
import json
import os
import re
from difflib import SequenceMatcher

import requests

from common import WORK, clients, config, sv

STOPWORDS = set("the of and for with in a an to on from by at as its their".split())


def similar(a, b):
    a, b = sv.title_key(a), sv.title_key(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    short, long_ = sorted((a, b), key=len)
    if len(short) >= 25 and short in long_:
        return 0.97
    return SequenceMatcher(None, a, b).ratio()


def main():
    arts = json.load(open(os.path.join(WORK, "email_articles.json"), encoding="utf-8"))
    fix = config()["fix"]                               # hand corrections: the ci_fix rows of the _rules table
    pm, oa = clients()
    fetch = pm.fetch

    def safe_fetch(pmids):                              # PubMed refuses a few PMIDs OpenAlex lists (400)
        try:
            return fetch(pmids)
        except requests.HTTPError:
            return []
    pm.fetch = safe_fetch

    def esearch(term, **kw):
        try:
            return pm._get("esearch.fcgi", db="pubmed", term=term, retmode="json", **kw).json()["esearchresult"].get("idlist", [])
        except (requests.HTTPError, ValueError, KeyError):
            return []

    out = os.path.join(WORK, "email_resolved.json")
    cache_path = out.replace(".json", ".cache.jsonl")
    done = {}
    if os.path.exists(cache_path):
        for ln in open(cache_path, encoding="utf-8"):
            r = json.loads(ln)
            done[(r["pdf"], r["citation"])] = r
    cache = open(cache_path, "a", encoding="utf-8")
    for n, a in enumerate(arts):
        if (a["pdf"], a["citation"]) in done:
            arts[n] = done[(a["pdf"], a["citation"])]
            continue
        a.update(next((v for k, v in fix.items() if a["citation"].startswith(k)), {}))
        title = a["title_guess"]
        pubmed, openalex, how_pm, how_oa = None, None, None, None
        if not a.get("not_literature"):
            # PubMed: the PMID linked in the email, or one found by DOI
            cands = [(a["pmid"], "email link")] if a.get("pmid") else []
            cands += [(p, "DOI") for d in a.get("dois") or [] for p in esearch(f"{d}[doi]")]
            for pmid, how in cands:
                recs = pm.fetch([pmid])
                if recs and similar(recs[0]["title"], title) >= 0.85:
                    pubmed, how_pm = recs[0], how
                    break
            # OpenAlex: by DOI, by PubMed's DOI or PMID, else title search
            for d in a.get("dois") or []:
                w = oa._get(f"works/doi:{d}")
                if w and similar(w.get("display_name"), title) >= 0.85:
                    openalex, how_oa = sv.parse_openalex_work(w), "DOI"
                    break
            if not openalex and pubmed and pubmed.get("doi"):    # PubMed's own DOI: trusted, like its PMID below
                w = oa._get(f"works/doi:{pubmed['doi']}")
                if w:
                    openalex, how_oa = sv.parse_openalex_work(w), "DOI (from PubMed)"
            if not openalex and pubmed:
                w = oa._get(f"works/pmid:{pubmed['pmid']}")
                if w:
                    openalex, how_oa = sv.parse_openalex_work(w), "PMID"
            if not openalex:
                j = oa._get("works", search=" ".join(sv.title_key(title).split()[:30]), filter="publication_year:2025-2027",
                            per_page=8) or {}
                best = max(((similar(w.get("display_name"), title), w) for w in j.get("results") or []),
                           key=lambda x: x[0], default=(0, None))
                if best[0] >= 0.85:
                    openalex, how_oa = sv.parse_openalex_work(best[1]), f"title search ({best[0]:.2f})"
            # PubMed again, through what OpenAlex knows, else by title words
            if not pubmed and openalex and openalex.get("pmid"):
                recs = pm.fetch([openalex["pmid"]])
                if recs and similar(recs[0]["title"], title) >= 0.85:
                    pubmed, how_pm = recs[0], "PMID from OpenAlex"
            if not pubmed and openalex and openalex.get("doi"):
                ids = esearch(f"{openalex['doi']}[doi]")
                recs = pm.fetch(ids[:1]) if ids else []
                if recs and similar(recs[0]["title"], title) >= 0.85:
                    pubmed, how_pm = recs[0], "DOI from OpenAlex"
            if not pubmed:
                ws = [w for w in re.findall(r"[A-Za-z0-9]+", title) if len(w) > 2][:14]
                ids = esearch(" AND ".join(f"{w}[ti]" for w in ws) + " AND 2025:2027[dp]", retmax=5) if ws else []
                pubmed, how_pm = next(((r, "title search") for r in pm.fetch(ids) if similar(r["title"], title) >= 0.85),
                                      (None, None)) if ids else (None, None)
            # second, looser pass: the first eight distinctive words, any field; OpenAlex title.search
            if not pubmed:
                ws = [w for w in re.findall(r"[A-Za-z]+", title) if w.lower() not in STOPWORDS and len(w) > 3][:8]
                ids = esearch(" AND ".join(ws) + " AND 2025:2027[dp]", retmax=10) if ws else []
                pubmed, how_pm = next(((r, "title search (loose)") for r in pm.fetch(ids) if similar(r["title"], title) >= 0.8),
                                      (None, None)) if ids else (None, None)
            if not openalex and pubmed:                         # PubMed found it by title: OpenAlex by its DOI or PMID
                w = (oa._get(f"works/doi:{pubmed['doi']}") if pubmed.get("doi") else None) or oa._get(f"works/pmid:{pubmed['pmid']}")
                if w:
                    openalex, how_oa = sv.parse_openalex_work(w), "DOI or PMID (from PubMed)"
            if not openalex:
                j = oa._get("works", filter=f"title.search:{' '.join(sv.title_key(title).split()[:12])}", per_page=10) or {}
                w = next((w for w in j.get("results") or [] if similar(w.get("display_name"), title) >= 0.8), None)
                if w:
                    openalex, how_oa = sv.parse_openalex_work(w), "title.search (loose)"
        a["in_pubmed"], a["in_openalex"] = bool(pubmed), bool(openalex)
        a["pubmed_match"], a["openalex_match"] = how_pm, how_oa
        a["pubmed"], a["openalex"] = pubmed, openalex
        cache.write(json.dumps(a, ensure_ascii=False) + "\n")
        cache.flush()
        print(f"{a['email_item']:3d} PM={'Y' if pubmed else '-'} OA={'Y' if openalex else '-'}  {title[:90]}", flush=True)
    json.dump(arts, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
