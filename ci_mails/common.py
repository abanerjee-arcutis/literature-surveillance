"""Shared by the ci_mails scripts: paths, the surveillance module with patient HTTP clients, and loading the emailed
articles together with the L1 pool they were chosen from."""
import json
import os
import re
import sys
import time
from difflib import SequenceMatcher

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
WORK = os.path.join(HERE, "work")                     # intermediate files (gitignored)
sys.path.insert(0, ROOT)
import surveillance as sv  # noqa: E402

VERBOSE = "--verbose" in sys.argv
TERMS = sv.clean_terms(sv.read_csv(os.path.join(ROOT, "terms.csv")))


def retrying(get):
    """Retry a client's _get on dropped connections and 5xx answers (OpenAlex sends 504s under load)."""
    def wrapped(self, *a, **k):
        for attempt in range(6):
            try:
                return get(self, *a, **k)
            except (requests.ConnectionError, requests.Timeout):
                pass
            except requests.HTTPError as e:
                if e.response is None or e.response.status_code < 500:
                    raise
            time.sleep(3 * (attempt + 1))
        return get(self, *a, **k)
    return wrapped


sv.PubMed._get = retrying(sv.PubMed._get)
sv.OpenAlex._get = retrying(sv.OpenAlex._get)


def clients():
    """PubMed without a key (3 requests/s) and OpenAlex with the key in OPENALEX_API_KEY, if set."""
    return sv.PubMed(None, None), sv.OpenAlex(None, os.environ.get("OPENALEX_API_KEY"))


def record_of(a):
    """The best metadata for an email article: PubMed, else OpenAlex, else the email's own citation."""
    return a.get("pubmed") or a.get("openalex") or dict(
        title=a["title_guess"], abstract=None, pub_types=[], mesh=[], journal="", pub_date="", doi=None, pmid=None)


def l1_terms_matching(title, abstract):
    text = f"{title} {abstract or ''}"
    return [t["term"] for t in TERMS if sv.query_matches(t["openalex_query"] or sv.plain_query(t["pubmed_query"]), text)]


def best_title(tk, candidates, threshold):
    """(value, similarity) of the candidate {title key: value} most similar to tk, if at least threshold, else (None, 0).
    quick_ratio() is an upper bound of ratio(), so most candidates are ruled out cheaply."""
    best, best_s = None, 0.0
    for t, v in candidates.items():
        sm = SequenceMatcher(None, t, tk)
        if sm.real_quick_ratio() < threshold or sm.quick_ratio() < threshold:
            continue
        s = sm.ratio()
        if s >= threshold and s > best_s:
            best, best_s = v, s
    return best, best_s


def load():
    """(emailed articles, L1 pool {key: record}, {pool key: emailed article} for the pool articles that were emailed)."""
    emails = json.load(open(os.path.join(WORK, "email_resolved.json"), encoding="utf-8"))
    pool = json.load(open(os.path.join(WORK, "l1_pool.json"), encoding="utf-8"))
    by_pmid = {r["pmid"]: k for k, r in pool.items() if r.get("pmid")}
    by_doi = {sv.norm_doi(r["doi"]): k for k, r in pool.items() if r.get("doi")}
    by_title = {sv.title_key(r["title"]): k for k, r in pool.items()}
    for a in emails:
        r = a["rec"] = record_of(a)
        k = by_pmid.get(r.get("pmid")) or by_doi.get(sv.norm_doi(r.get("doi"))) or by_title.get(sv.title_key(r["title"]))
        if not k:                                       # email titles are retyped: 'Two' for '2', 'Following' for 'after'
            k, s = best_title(sv.title_key(r["title"]), by_title, 0.82)
            if k and VERBOSE:
                print(f"  fuzzy {s:.2f}: {r['title'][:70]} = {pool[k]['title'][:70]}")
        a["pool_key"] = k
        a["l1_terms_text"] = l1_terms_matching(r["title"], r.get("abstract"))
    flagged = {a["pool_key"]: a for a in emails if a.get("pool_key")}
    # a second pool record of an emailed article (another source's copy, reworded title) counts as emailed too
    keys = {sv.title_key(a["rec"]["title"]): a for a in emails if not a.get("not_literature")}
    for k, r in pool.items():
        if k not in flagged:
            a, s = best_title(sv.title_key(r["title"]), keys, 0.9)
            if a:
                flagged[k] = a
                if VERBOSE:
                    print(f"  alias {s:.2f}: {r['title'][:70]} = {a['rec']['title'][:70]}")
    return emails, pool, flagged


def study(r):
    return sv.tag_study_type(r.get("pub_types") or [], f"{r['title']} {r.get('abstract') or ''}")


def funding_text(r):
    """Conflict-of-interest statement and funders, which the Arcutis-funded rule reads besides the abstract."""
    return f"{r.get('coi') or ''} {' '.join(r.get('grants') or [])}"


def words(text):
    return " ".join(re.findall(r"[a-z0-9]+", (text or "").lower()))
