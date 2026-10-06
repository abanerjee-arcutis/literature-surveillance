#!/usr/bin/env python3
"""Literature surveillance across PubMed, OpenAlex and Google Scholar.

    python surveillance.py --api-key YOUR_KEY --serpapi-key YOUR_SERPAPI_KEY --days 90

Reads terms.csv and exclusions.csv, searches each source for each term over the date window,
de-duplicates the hits into data/articles.json (a growing local store, one record per article:
same PMID, same DOI or same title means same article), applies the exclusion rules, tags disease /
product / study type / access level, writes rule-based summaries from the abstract, and renders
literature_update.html from app/template.html.

Re-running only fetches new articles; articles already in the store are kept. Reviewer decisions
in data/decisions.csv (pmid,decision: accepted / ignored / restored) set which feed an article is on;
its optional reasons column (the keywords and reasons ticked, separated by ';') is shown on the page.
Search terms and sources saved from the page's Settings (data/settings.json) replace terms.csv and the
default --sources / --scholar-pages; its L3 priority keywords make the articles that mention them High priority.

Google Scholar has no public API, so it is queried through SerpApi and skipped when no SerpApi key
is given. As a Databricks job, keys come from a secret scope (--secret-scope), and with --tables the store,
decisions, settings and a run log live in Unity Catalog tables (tables.sql) instead of files: no page is written,
the Databricks app builds it from the tables.

Only dependency: requests (pip install requests).
"""
from __future__ import annotations

import argparse
import html
import csv
import hashlib
import json
import os
import re
import sys
import time
import unicodedata
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from difflib import SequenceMatcher
from xml.etree import ElementTree as ET

import requests

HERE = os.path.dirname(os.path.abspath(__file__ if "__file__" in globals() else sys.argv[0]))
sys.path.insert(0, os.path.join(HERE, "app"))         # the page template and the code that fills it live with the app
from page import TEMPLATE, fill as fill_page, header as page_header  # noqa: E402
EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
OPENALEX = "https://api.openalex.org/"
SERPAPI = "https://serpapi.com/search.json"
SOURCES = ["PubMed", "OpenAlex", "Google Scholar"]     # also the preference order for metadata when records merge
SOURCE_IDS = ["pubmed", "openalex", "scholar"]           # the same sources as named in --sources and settings.json
TERM_FIELDS = ["term", "product_category", "pubmed_query", "openalex_query", "scholar_query"]
SCHOLAR_FIELDS = ("scholar_id", "scholar_link", "scholar_snippet", "scholar_via")

DISEASES = {
    "Psoriasis": r"psoria",
    "Atopic Dermatitis": r"atopic|eczema|hand dermatitis|hand eczema",
    "Seb Derm": r"seborrh",
    "HS": r"hidradenitis",
    "Vitiligo": r"vitiligo",
}
STUDY_TYPES = [  # PubMed PublicationType → label, first match wins
    ("Retraction of Publication", "Retraction notice"), ("Retracted Publication", "Retracted"),
    ("Published Erratum", "Erratum"), ("Preprint", "Preprint"), ("Comment", "Comment"),
    ("Congress", "Conference abstract"), ("Book chapter", "Book chapter"),
    ("Clinical Trial, Phase III", "Phase 3"), ("Clinical Trial, Phase II", "Phase 2"),
    ("Randomized Controlled Trial", "RCT"), ("Meta-Analysis", "Meta-analysis"),
    ("Systematic Review", "Systematic review"), ("Review", "Review"),
    ("Case Reports", "Case report"), ("Observational Study", "Real-world"),
    ("Comparative Study", "Comparative"), ("Letter", "Letter"), ("Editorial", "Editorial"),
]
OPENALEX_TYPES = {  # OpenAlex work type → the PubMed PublicationType it corresponds to
    "review": "Review", "letter": "Letter", "editorial": "Editorial", "erratum": "Published Erratum",
    "preprint": "Preprint", "book-chapter": "Book chapter", "retraction": "Retraction of Publication",
}
INTENTS = ["Safety", "Efficacy", "Real-world use", "Mechanism", "Disease state", "Patient perspective",
           "Review & guidance", "Other"]
INTENT_RULES = [  # (intent, study types, title pattern, unless the title matches): the first rule that matches wins
    ("Mechanism", {"Preclinical"}, r"\b(mice|mouse|murine|rats?|in vitro|in vivo|in silico|cell lines?)\b", None),
    # adverse events, unless the title is about efficacy or treating the event ('Efficacy and safety of …' is a trial)
    ("Safety", (), r"\b(adverse|side.effects?|toxicit\w*|tolerab\w*|safety (of|profile|signals?|concerns?|data|outcomes?)"
                   r"|long.term safety|allergic|hypersensitivit\w*|withdrawal|cushing|herpeticum|malignan\w*|pharmacovigilance"
                   r"|boxed warning)\b|\b\w+-(induced|associated|related)\b"
                   r"|\b(after|following)\b[^:;.]{0,60}?\b(use|therapy|treatment|injection|exposure)\b",
     r"\b(efficacy|effectiveness|managed|treated|successful\w*|treatment of|management of)\b"),
    ("Review & guidance", {"Review", "Systematic review", "Meta-analysis", "Book chapter", "Editorial", "Comment"},
     r"\b((?<!chart )reviews?|consensus|guidelines?|recommendations|expert opinion|delphi|bibliometric)\b", None),
    ("Mechanism", (), r"\b(mechanisms?|pathways?|pharmacolog\w*|pharmacokinetic\w*|physicochemical|formulation|nano\w*"
                      r"|permeation|membranes?|transcriptom\w*|oxidation|immunopatholog\w*)\b", r"\b(efficacy|effectiveness)\b"),
    ("Efficacy", {"Phase 3", "Phase 2", "RCT"},
     r"\b(randomi[sz]ed|trials?|phase (2|3|ii|iii)|double.blind|vehicle.controlled|open.label)\b", None),
    ("Real-world use", (), r"\breal.world\b", None),
    ("Disease state", (), r"\b(prevalence|incidence|epidemiolog\w*|burden|comorbid\w*|risk of|quality.of.life|psychiatric"
                          r"|mental health|depressi\w*|anxiety)\b", None),
    ("Real-world use", {"Real-world", "Comparative"}, r"\b(retrospective|registry|claims|database|cohort|chart review"
                                                      r"|electronic health|utili[sz]ation|prescri\w*|costs?|medicare|medicaid)\b", None),
    ("Patient perspective", {"Survey"}, r"\b(survey|preferences?|patient experience|perspectives?|perceptions?|satisfaction"
                                        r"|adherence|qualitative|questionnaire|social media)\b", None),
    ("Disease state", (), r"\b(associat\w*|diagnos\w*|mimick\w*|masked|pitfall|manifestations?|presentation|rare"
                          r"|pathogen\w*|pathophysiolog\w*|phenotyp\w*)\b", None),
    ("Efficacy", (), r"\b(efficacy|effective\w*|successful\w*|improv\w*|respon\w*|treated|treatment|treating|clearance"
                     r"|resolution|remission|repigmentation|outcomes?|managed)\b", None),
]
INTENT_ABSTRACT_RULES = [  # used only when the title and study type say nothing
    ("Efficacy", r"\b(randomi[sz]ed|double.blind|vehicle.controlled|phase (2|3|ii|iii))\b"),
    ("Real-world use", r"\b(real.world|retrospective|registry|claims|cohort)\b"),
    ("Mechanism", r"\b(mice|murine|in vitro|in vivo)\b"),
]
KEYWORDS = {  # label → pattern in the title or abstract; the page offers them as reasons when a reviewer accepts or ignores
    # treatments
    "Roflumilast": r"\broflumilast\b", "Tapinarof": r"\btapinarof\b", "Ruxolitinib": r"\bruxolitinib\b",
    "Delgocitinib": r"\bdelgocitinib\b", "Orismilast": r"\borismilast\b", "Crisaborole": r"\bcrisaborole\b",
    "Apremilast": r"\bapremilast\b",
    "Topical corticosteroids": r"\b(topical (cortico)?steroids?|TCS)\b",
    "Calcineurin inhibitors": r"\b(tacrolimus|pimecrolimus|calcineurin inhibitors?|TCIs?)\b",
    "Biologics": r"\b(biologics?|dupilumab|tralokinumab|lebrikizumab|nemolizumab|secukinumab|ixekizumab|bimekizumab"
                 r"|risankizumab|guselkumab|tildrakizumab|ustekinumab|adalimumab|etanercept|infliximab)\b",
    "Oral JAK / TYK2 inhibitors": r"\b(upadacitinib|abrocitinib|baricitinib|tofacitinib|deucravacitinib|oral JAK)\b",
    "Phototherapy": r"\b(phototherapy|NB-?UVB|narrow.?band|excimer)\b",
    # who
    "Pediatric": r"\b(pa?ediatric|children|child|adolescents?|juvenile)\b",
    "Infants": r"\b(infants?|infantile|toddlers?|neonat\w*)\b",
    "Older adults": r"\b(elderly|older (adults|patients)|geriatric)\b",
    "Skin of color": r"\b(skin of colou?r|black (patients|individuals|adults|children)|african.american|hispanic"
                     r"|fitzpatrick (skin )?(types? )?(iv|v|vi))\b",
    "Pregnancy": r"\b(pregnan\w*|lactat\w*|breast.?feed\w*)\b",
    # where
    "Face": r"\b(face|facial|eyelids?|periorbital|perioral)\b",
    "Scalp": r"\bscalp\b",
    "Intertriginous / genital": r"\b(intertrigin\w*|genital\w*|inverse psoriasis|skin folds?|flexur\w*)\b",
    "Hands": r"\b(hand (eczema|dermatitis)|palms?|palmoplantar)\b",
    "Nails": r"\bnails?\b",
    # evidence
    "Long-term": r"\b(long.term|open.label extension|(5[2-9]|[6-9]\d|1\d\d)[- ]weeks?)\b",
    "Head-to-head": r"\b(head.to.head|(versus|vs\.?|compared (with|to)) (?!vehicle|placebo|baseline|controls?|healthy)[a-z])",
    "Pooled / post hoc": r"\b(pooled|post.hoc|subgroup analys[ie]s)\b",
    "Itch": r"\b(prurit\w*|itch\w*)\b",
    "Quality of life": r"\b(quality.of.life|C?DLQI)\b",
}


# ----------------------------------------------------------------------------- queries
def plain_query(pubmed_query: str) -> str:
    """PubMed syntax → plain boolean query for OpenAlex / Google Scholar: field tags and truncation
    dropped, multi-word terms quoted. 'topical steroid withdrawal[tiab]' → '"topical steroid withdrawal"'."""
    out = []
    for tok in re.split(r"(\(|\)|\bAND\b|\bOR\b|\bNOT\b)", pubmed_query):
        tok = tok.strip()
        if tok in ("(", ")", "AND", "OR", "NOT"):
            out.append(tok)
        elif tok:
            phrase = re.sub(r"\[[^\]]*\]", "", tok).replace("*", "").strip().strip('"')
            out.append(f'"{phrase}"' if " " in phrase else phrase)
    return re.sub(r"\(\s+", "(", re.sub(r"\s+\)", ")", " ".join(out)))


def scholar_query(plain: str) -> str:
    """Google Scholar ANDs words by default and negates with '-'."""
    return re.sub(r"\s+", " ", re.sub(r"\bNOT\s+", "-", re.sub(r"\bAND\b", " ", plain))).strip()


def query_matches(query: str, text: str) -> bool:
    """Evaluates a plain boolean query (AND, OR, NOT, parentheses, "phrases"; adjacent terms are ANDed)
    against text. Words also match as prefixes, like PubMed truncation: 'inhibitor' finds 'inhibitors'."""
    toks = re.findall(r'"[^"]*"|\(|\)|[^\s()"]+', query or "")
    hay = " ".join(re.findall(r"[a-z0-9]+", (text or "").lower()))
    pos = 0

    def peek():
        return toks[pos] if pos < len(toks) else None

    def atom():
        nonlocal pos
        tok = peek()
        if tok is None or tok == ")":
            return False
        pos += 1
        if tok == "(":
            value = any_of()
            if peek() == ")":
                pos += 1
            return value
        words = re.findall(r"[a-z0-9]+", tok.lower())
        return bool(words) and re.search(r"\b" + r"\w*\s".join(map(re.escape, words)), hay) is not None

    def negated():
        nonlocal pos
        if peek() == "NOT":
            pos += 1
            return not negated()
        return atom()

    def all_of():
        nonlocal pos
        value = negated()
        while peek() not in (None, ")", "OR"):
            if peek() == "AND":
                pos += 1
            value = negated() and value
        return value

    def any_of():
        nonlocal pos
        value = all_of()
        while peek() == "OR":
            pos += 1
            value = all_of() or value
        return value

    return bool(toks) and any_of()


# ----------------------------------------------------------------------------- PubMed
class PubMed:
    def __init__(self, api_key: str | None, email: str | None):
        self.s = requests.Session()
        self.s.headers["User-Agent"] = f"LitSurveillance/0.3 ({email or 'no-email'})"
        self.api_key = api_key
        self.delay = 0.11 if api_key else 0.34          # 10/s with a key, 3/s without

    def _get(self, endpoint: str, **params):
        if self.api_key:
            params["api_key"] = self.api_key
        for attempt in range(4):
            time.sleep(self.delay)
            r = self.s.get(EUTILS + endpoint, params=params, timeout=60)
            if r.status_code in (429, 500, 502, 503):
                time.sleep(2 ** attempt)
                continue
            r.raise_for_status()
            return r
        r.raise_for_status()

    def search(self, query: str, since: date, until: date, retmax: int = 500) -> list[str]:
        j = self._get("esearch.fcgi", db="pubmed", term=query, datetype="edat",
                      mindate=since.strftime("%Y/%m/%d"), maxdate=until.strftime("%Y/%m/%d"),
                      retmax=retmax, retmode="json").json()
        return j.get("esearchresult", {}).get("idlist", [])

    def fetch(self, pmids: list[str]) -> list[dict]:
        out = []
        for i in range(0, len(pmids), 100):
            xml = self._get("efetch.fcgi", db="pubmed", id=",".join(pmids[i:i + 100]), retmode="xml").content
            out.extend(parse_pubmed_xml(xml))
        return out

    def fetch_pmc(self, pmcid: str) -> dict:
        """Full text of an open-access PMC article via E-utilities (db=pmc). Empty dict if not available."""
        try:
            xml = self._get("efetch.fcgi", db="pmc", id=pmcid.replace("PMC", ""), retmode="xml").content
            return parse_pmc_xml(xml)
        except Exception as e:
            print(f"  PMC fetch failed for {pmcid}: {e}", file=sys.stderr)
            return {}


def strip_markers(root):
    """Remove <xref> and <sup> elements (citation markers) but keep the text that follows them."""
    for parent in list(root.iter()):
        for child in list(parent):
            if child.tag in ("xref", "sup"):
                tail = child.tail or ""
                idx = list(parent).index(child)
                if idx == 0:
                    parent.text = (parent.text or "") + tail
                else:
                    prev = parent[idx - 1]
                    prev.tail = (prev.tail or "") + tail
                parent.remove(child)


def parse_pmc_xml(xml: bytes) -> dict:
    """Returns {'abstract': str|None, 'body': str|None, 'sections': [(title, text)]} from a PMC article XML."""
    root = ET.fromstring(xml)
    art = root.find(".//article")
    if art is None:
        return {}
    strip_markers(art)                             # citation markers otherwise leak as stray digits
    def text_of(el):
        return clean_text(re.sub(r"\s+([,.;:)])", r"\1", " ".join(el.itertext())))
    abstract = None
    ab = art.find(".//front//abstract")
    if ab is not None:
        abstract = " ".join(text_of(p) for p in ab.findall(".//p")) or text_of(ab) or None
    sections, paras = [], []
    body = art.find(".//body")
    if body is not None:
        for sec in body.findall("./sec"):
            title = clean_text("".join((sec.find("title").itertext()) if sec.find("title") is not None else ""))
            txt = " ".join(text_of(p) for p in sec.findall(".//p"))
            if txt:
                sections.append((title, txt))
        for p in body.findall(".//p"):
            t = text_of(p)
            if len(t) > 40:
                paras.append(t)
    body_txt = " ".join(paras)[:40000] or None
    return {"abstract": abstract, "body": body_txt, "sections": sections[:20]}


MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def pub_date_parts(art):
    for base in (".//ArticleDate", ".//Journal/JournalIssue/PubDate"):
        y = art.findtext(f"{base}/Year")
        if not y:
            md = art.findtext(f"{base}/MedlineDate") or ""
            mm = re.match(r"(\d{4})", md)
            y = mm.group(1) if mm else None
        if y:
            m = (art.findtext(f"{base}/Month") or "1").strip()
            m = MONTHS.get(m[:3].lower(), int(m) if m.isdigit() else 1)
            d = art.findtext(f"{base}/Day") or "1"
            return y, int(m), int(d) if d.isdigit() else 1
    return "", 1, 1


def clean_text(t: str) -> str:
    t = html.unescape(t or "").replace("\xa0", " ")
    return re.sub(r"\s+", " ", t).strip()


def parse_pubmed_xml(xml: bytes) -> list[dict]:
    root = ET.fromstring(xml)
    out = []
    for art in root.findall(".//PubmedArticle"):
        pmid = art.findtext(".//PMID")
        t = art.find(".//ArticleTitle")
        title = clean_text("".join(t.itertext())) if t is not None else ""
        # keep structured abstract labels: they become the key points
        sections = []
        for a in art.findall(".//Abstract/AbstractText"):
            label = a.get("Label") or a.get("NlmCategory") or ""
            sections.append((label.title(), clean_text("".join(a.itertext()))))
        abstract = " ".join(s for _, s in sections) or None
        ids = {e.get("IdType"): (e.text or "").strip() for e in art.findall("./PubmedData/ArticleIdList/ArticleId")}
        pmc = ids.get("pmc") or None
        if pmc and not pmc.upper().startswith("PMC"):
            pmc = "PMC" + pmc
        authors = [f"{a.findtext('LastName') or ''} {a.findtext('Initials') or ''}".strip()
                   for a in art.findall(".//Author") if a.findtext("LastName")]
        pt = [p.text for p in art.findall(".//PublicationType")]
        y, m, d = pub_date_parts(art)
        grants = [g.findtext("Agency") for g in art.findall(".//Grant") if g.findtext("Agency")]
        coi = art.findtext(".//CoiStatement") or ""
        issue = art.findtext(".//JournalIssue/Issue") or ""
        lang = art.findtext(".//Language") or "eng"
        mc = art.find("./MedlineCitation")
        status = (mc if mc is not None else art).get("Status") or ""
        out.append(dict(
            origin="PubMed", issue=issue, language=lang, record_status=status,
            pmid=pmid, title=title, abstract=abstract, sections=sections,
            authors=authors, journal=art.findtext(".//Journal/Title") or art.findtext(".//MedlineTA") or "",
            journal_abbrev=art.findtext(".//ISOAbbreviation") or "",
            pub_date=f"{y}-{m:02d}-{d:02d}" if y else "",
            doi=ids.get("doi"), pmc=pmc, pub_types=pt, grants=grants, coi=coi,
            url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
            mesh=[m.findtext("DescriptorName") for m in art.findall(".//MeshHeading")],
        ))
    # NCBI Bookshelf chapters come back as a different element; keep them so nothing is silently dropped
    for bk in root.findall(".//PubmedBookArticle"):
        pmid = bk.findtext(".//PMID")
        title = clean_text("".join(bk.find(".//ArticleTitle").itertext())) if bk.find(".//ArticleTitle") is not None else clean_text(bk.findtext(".//BookTitle") or "")
        abstract = " ".join(clean_text("".join(a.itertext())) for a in bk.findall(".//Abstract/AbstractText")) or None
        out.append(dict(origin="PubMed", pmid=pmid, title=title, abstract=abstract, sections=[], authors=[], journal=clean_text(bk.findtext(".//BookTitle") or "Book"),
                        journal_abbrev="Book", pub_date=(bk.findtext(".//PubDate/Year") or "") + "-01-01" if bk.findtext(".//PubDate/Year") else "",
                        doi=None, pmc=None, pub_types=["Book chapter"], grants=[], coi="", issue="", language="eng", record_status="",
                        url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/", mesh=[]))
    return out


# ----------------------------------------------------------------------------- OpenAlex
class OpenAlex:
    """OpenAlex works API. Free; a contact email puts requests in the faster 'polite pool'."""
    def __init__(self, email: str | None, api_key: str | None = None):
        self.s = requests.Session()
        self.s.headers["User-Agent"] = f"LitSurveillance/0.3 (mailto:{email or 'no-email'})"
        self.params = {k: v for k, v in (("mailto", email), ("api_key", api_key)) if v}

    def _get(self, path: str, **params):
        for attempt in range(4):
            time.sleep(0.11)
            r = self.s.get(OPENALEX + path, params={**self.params, **params}, timeout=60)
            if r.status_code == 404:
                return None
            if r.status_code in (429, 500, 502, 503):
                time.sleep(2 ** attempt)
                continue
            r.raise_for_status()
            return r.json()
        r.raise_for_status()

    def search(self, query: str, since: date, until: date, max_pages: int = 5) -> list[dict]:
        """Works published in the window that match the query anywhere OpenAlex indexes (title, abstract,
        full text). The caller keeps only title/abstract matches, like PubMed's [tiab]."""
        out, cursor = [], "*"
        for _ in range(max_pages):
            j = self._get("works", search=query, filter=f"from_publication_date:{since},to_publication_date:{until}",
                          per_page=200, cursor=cursor) or {}
            out += [parse_openalex_work(w) for w in j.get("results") or []]
            cursor = (j.get("meta") or {}).get("next_cursor")
            if not cursor or not j.get("results"):
                break
        return out

    def resolve(self, a: dict) -> dict | None:
        """The OpenAlex record for a Google Scholar hit: by DOI, else by title (same year, same numbers)."""
        t = title_key(a.get("title"))
        if a.get("doi"):
            w = self._get(f"works/doi:{a['doi']}")
            if w and SequenceMatcher(None, title_key(w.get("display_name")), t).ratio() >= 0.8:
                return parse_openalex_work(w)
        if len(t) < 20:
            return None
        j = self._get("works", filter=f"title.search:{t}", per_page=5) or {}
        for w in j.get("results") or []:
            b = parse_openalex_work(w)
            if same_title(title_key(b["title"]), t, a.get("title_truncated")) and years_close(a, b):
                return b
        return None


def abstract_from_index(inverted: dict | None) -> str | None:
    """OpenAlex ships abstracts as {word: [positions]}; put the words back in order."""
    if not inverted:
        return None
    words = sorted((p, w) for w, ps in inverted.items() for p in ps)
    return clean_text(" ".join(w for _, w in words)) or None


def author_short(name: str | None) -> str:
    """'Jane A. Smith' or 'JA Smith' → 'Smith JA', the PubMed style used on the page."""
    parts = (name or "").replace(".", " ").split()
    if len(parts) < 2:
        return name or ""
    initials = "".join(p if p.isupper() and len(p) <= 3 else p[0] for p in parts[:-1])
    return f"{parts[-1]} {initials.upper()}"


def parse_openalex_work(w: dict) -> dict:
    ids = w.get("ids") or {}
    pmid = re.sub(r"\D", "", ids.get("pmid") or "") or None
    pmcid = re.sub(r"\D", "", ids.get("pmcid") or "")
    loc = w.get("primary_location") or {}
    doi = norm_doi(w.get("doi"))
    pub_types = [OPENALEX_TYPES[w["type"]]] if w.get("type") in OPENALEX_TYPES else []
    if w.get("is_retracted"):
        pub_types.insert(0, "Retracted Publication")
    funders = [g.get("funder_display_name") for g in w.get("grants") or []] + [f.get("display_name") for f in w.get("funders") or []]
    lang = w.get("language") or "en"
    return dict(
        origin="OpenAlex", pmid=pmid, title=clean_text(re.sub(r"<[^>]+>", "", w.get("display_name") or w.get("title") or "")),
        abstract=abstract_from_index(w.get("abstract_inverted_index")), sections=[],
        authors=[author_short(x["author"]["display_name"]) for x in w.get("authorships") or [] if (x.get("author") or {}).get("display_name")],
        journal=clean_text((loc.get("source") or {}).get("display_name") or ""), journal_abbrev="",
        pub_date=w.get("publication_date") or "", doi=doi, pmc=f"PMC{pmcid}" if pmcid else None,
        pub_types=pub_types, grants=list(dict.fromkeys(f for f in funders if f)), coi="",
        issue=(w.get("biblio") or {}).get("issue") or "", language="eng" if lang == "en" else lang, record_status="",
        url=f"https://doi.org/{doi}" if doi else loc.get("landing_page_url") or w.get("id") or "",
        mesh=[], openalex_id=(w.get("id") or "").rsplit("/", 1)[-1] or None,
        oa_url=(w.get("open_access") or {}).get("oa_url"),
    )


# ----------------------------------------------------------------------------- Google Scholar
class Scholar:
    """Google Scholar through SerpApi (serpapi.com). Google offers no Scholar API and blocks direct scraping,
    so a SerpApi key is needed; every page of results costs one SerpApi search."""
    def __init__(self, api_key: str):
        self.s = requests.Session()
        self.api_key = api_key

    def search(self, query: str, since: date, until: date, max_pages: int = 1) -> list[dict]:
        """Newest first (Scholar's 'sort by date', which covers about the past year). A hit is kept while
        the date Scholar added it ('3 days ago') falls inside the window."""
        out = []
        for page in range(max_pages):
            r = self.s.get(SERPAPI, params=dict(engine="google_scholar", q=query, hl="en", scisbd=2, as_vis=1,
                                               num=20, start=20 * page, api_key=self.api_key), timeout=90)
            j = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            if j.get("error"):
                if "hasn't returned any results" in j["error"]:
                    break
                raise RuntimeError(j["error"])
            r.raise_for_status()
            results = j.get("organic_results") or []
            past_window = False
            for x in results:
                a = parse_scholar_result(x)
                age = a.pop("age_days")
                added = date.today() - timedelta(days=age) if age is not None else None
                if added and added < since:
                    past_window = True
                elif not (added and added > until):
                    out.append(a)
            if past_window or len(results) < 20:
                break
        return out


def doi_from_url(url: str | None) -> str | None:
    """DOI inside a publisher URL (Springer, Wiley, T&F, Frontiers, SAGE…), without a trailing /full, /pdf etc."""
    m = re.search(r"10\.\d{4,9}/[^\s?#&]+", urllib.parse.unquote(url or ""))
    if not m:
        return None
    return norm_doi(re.sub(r"(/(full|abstract|abs|pdf|epdf|fulltext|full-text|html|meta|summary))+/?$|\.pdf$", "", m.group(0), flags=re.I))


def parse_scholar_result(r: dict) -> dict:
    """One SerpApi organic result. Scholar gives a title, a link, a snippet and a one-line citation
    ('GL Waldal, A Smith - Dermatitis, 2026 - liebertpub.com'), but no abstract."""
    info = r.get("publication_info") or {}
    summary = clean_text(info.get("summary") or "")
    parts = [p.strip() for p in summary.split(" - ")]
    names = [x["name"] for x in info.get("authors") or [] if x.get("name")]
    if not names and len(parts) > 1:
        names = [n.strip() for n in parts[0].split(",") if n.strip() and "…" not in n]
    years = re.findall(r"\b(?:19|20)\d{2}\b", summary)
    snippet = clean_text(r.get("snippet") or "")
    age = re.match(r"(\d+)\s+days?\s+ago\s*[-–—]\s*", snippet)
    title = clean_text(re.sub(r"^\s*\[(HTML|PDF|CITATION|BOOK|B)\]\s*", "", r.get("title") or "", flags=re.I))
    link = r.get("link") or ""
    return dict(
        origin="Google Scholar", pmid=None, title=title.rstrip("…").strip(), title_truncated=title.endswith("…"),
        abstract=None, sections=[], authors=[author_short(n) for n in names],
        journal=re.sub(r",?\s*(?:19|20)\d{2}$", "", parts[1]).strip(" ,…") if len(parts) > 2 else "", journal_abbrev="",
        pub_date=f"{years[-1]}-01-01" if years else "", doi=doi_from_url(link), pmc=None, pub_types=[], grants=[],
        coi="", issue="", language="eng", record_status="", url=link, mesh=[],
        scholar_id=r.get("result_id"), scholar_link=link, scholar_snippet=snippet[age.end():] if age else snippet,
        scholar_via=parts[-1] if len(parts) > 2 else "", age_days=int(age.group(1)) if age else None,
    )


# ----------------------------------------------------------------------------- de-duplication
def norm_doi(doi: str | None) -> str | None:
    d = re.sub(r"^(https?://(dx\.)?doi\.org/|doi:\s*)", "", (doi or "").strip(), flags=re.I).lower().rstrip(".")
    return d or None


def title_key(title: str | None) -> str:
    """Lower-case ASCII words only, so markup, punctuation, accents and PubMed's [brackets] don't matter."""
    t = unicodedata.normalize("NFKD", re.sub(r"<[^>]+>", " ", html.unescape(title or "")))
    t = "".join(c for c in t if not unicodedata.combining(c)).lower()
    return " ".join(re.findall(r"[a-z0-9]+", t))


def same_title(a: str, b: str, truncated: bool = False) -> bool:
    """Two title keys name the same article: equal; or nearly equal with the same numbers (so 'phase 2'
    never matches 'phase 3'); or, when Google Scholar cut one short, a prefix of the other."""
    if not a or not b:
        return False
    if a == b:
        return True
    short, long_ = sorted((a, b), key=len)
    if truncated:
        return len(short) >= 40 and long_.startswith(short)
    if re.findall(r"\d+", a) != re.findall(r"\d+", b):
        return False
    return len(short) >= 30 and SequenceMatcher(None, a, b).ratio() >= 0.95


def years_close(x: dict, y: dict) -> bool:
    a, b = (x.get("pub_date") or "")[:4], (y.get("pub_date") or "")[:4]
    return not (a.isdigit() and b.isdigit()) or abs(int(a) - int(b)) <= 1


def compatible(x: dict, y: dict) -> bool:
    """False when the two records carry different PMIDs or DOIs, i.e. are certainly different articles."""
    if x.get("pmid") and y.get("pmid") and x["pmid"] != y["pmid"]:
        return False
    dx, dy = norm_doi(x.get("doi")), norm_doi(y.get("doi"))
    return not (dx and dy and not (dx.startswith(dy) or dy.startswith(dx)))


class Index:
    """Finds the stored record that is the same article as an incoming one: by PMID, then DOI, then title."""
    def __init__(self, store: dict):
        self.store, self.pmid, self.doi, self.title = store, {}, {}, {}
        for k, a in store.items():
            self.add(k, a)

    def add(self, key: str, a: dict):
        if a.get("pmid"):
            self.pmid[a["pmid"]] = key
        if norm_doi(a.get("doi")):
            self.doi[norm_doi(a["doi"])] = key
        t = title_key(a.get("title"))
        if t:                                          # titles are compared within blocks sharing the first 3 words
            self.title.setdefault(" ".join(t.split()[:3]), {})[t] = key

    def find(self, a: dict) -> str | None:
        for k in (self.pmid.get(a.get("pmid") or ""), self.doi.get(norm_doi(a.get("doi")) or "")):
            if k in self.store and compatible(self.store[k], a):
                return k
        t = title_key(a.get("title"))
        for t2, k in (self.title.get(" ".join(t.split()[:3]), {}) if t else {}).items():
            b = self.store.get(k)
            if b and same_title(t, t2, a.get("title_truncated") or b.get("title_truncated")) and compatible(b, a) and years_close(b, a):
                return k
        return None


def record_key(a: dict) -> str:
    """The store key: the PMID for PubMed records, otherwise a prefixed DOI, OpenAlex id or Scholar id."""
    if a["origin"] == "PubMed":
        return a["pmid"]
    if norm_doi(a.get("doi")):
        return "doi:" + norm_doi(a["doi"])
    if a.get("openalex_id"):
        return "openalex:" + a["openalex_id"]
    return "scholar:" + (a.get("scholar_id") or hashlib.sha1(title_key(a["title"]).encode()).hexdigest()[:12])


def sort_sources(names) -> list[str]:
    return sorted(set(names), key=SOURCES.index)


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def merge(store: dict, idx: Index, new: dict) -> str:
    """Adds `new` to the store, folding it into the stored record for the same article if there is one.
    The copy from the better source (PubMed > OpenAlex > Google Scholar; the newer one on a tie) keeps its
    metadata; feeds, matched terms, identifiers, links and the earliest first_seen are pooled. If the key
    changes (a Scholar or OpenAlex record later found in PubMed), the old key is kept in `aliases`."""
    new.setdefault("sources", [new["origin"]])
    new.setdefault("matched_terms", [])
    key = idx.find(new)
    if key is None:
        new.setdefault("first_seen", now())
        key = record_key(new)
        store[key] = new
        idx.add(key, new)
        return key
    old = store.pop(key)
    keep, other = (new, old) if SOURCES.index(new["origin"]) <= SOURCES.index(old["origin"]) else (old, new)
    for f in ("pmid", "doi", "pmc", "pmc_text", "oa_url", "openalex_id", *SCHOLAR_FIELDS):
        if not keep.get(f) and other.get(f):
            keep[f] = other[f]
    keep["sources"] = sort_sources(set(keep["sources"]) | set(other.get("sources", [])))
    keep["matched_terms"] = sorted(set(keep["matched_terms"]) | set(other.get("matched_terms", [])))
    keep["first_seen"] = min(filter(None, (keep.get("first_seen"), other.get("first_seen"))), default=now())
    new_key = record_key(keep)
    keep["aliases"] = sorted((set(keep.get("aliases", [])) | set(other.get("aliases", [])) | {key}) - {new_key})
    store[new_key] = keep
    idx.add(new_key, other)
    idx.add(new_key, keep)
    return new_key


def note_hit(hits: dict, pmid: str, term: str, feed: str):
    h = hits.setdefault(pmid, {"terms": set(), "feeds": set()})
    h["terms"].add(term)
    h["feeds"].add(feed)


def ingest_pubmed(pm: PubMed, store: dict, idx: Index, hits: dict, extra=(), rebuild=False) -> tuple[list[str], list[str]]:
    """Fetches the PMIDs in `hits` ({pmid: {terms, feeds}}) that are not stored yet, plus `extra` (stored
    records to refresh), and merges them into the store; stored PMIDs only gain the new terms and feeds.
    Returns (new PMIDs, PMIDs PubMed did not return)."""
    new_ids = [p for p in hits if p not in store]
    got = pm.fetch(new_ids + [p for p in extra if p not in new_ids]) if (new_ids or extra) else []
    for a in got:
        old = store.get(a["pmid"], {})
        if a.get("pmc"):
            had_body = (old.get("pmc_text") or {}).get("body")
            a["pmc_text"] = old.get("pmc_text") if (had_body and not rebuild) else pm.fetch_pmc(a["pmc"])
        h = hits.get(a["pmid"], {})
        a["matched_terms"] = sorted(h.get("terms", ()))
        a["sources"] = sort_sources(h.get("feeds", ()))
        merge(store, idx, a)
    for pmid, h in hits.items():                      # a known article may match a new term or feed
        if pmid in store:
            a = store[pmid]
            a["matched_terms"] = sorted(set(a.get("matched_terms", [])) | h["terms"])
            a["sources"] = sort_sources(set(a.get("sources", [])) | h["feeds"])
    return new_ids, sorted(set(new_ids) - {a["pmid"] for a in got})


# ----------------------------------------------------------------------------- tagging
def read_csv(path):
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def clean_terms(terms) -> list[dict]:
    """Term rows with exactly the TERM_FIELDS, trimmed; rows without a name or PubMed query are dropped."""
    rows = [{k: str(t.get(k) or "").strip() for k in TERM_FIELDS} for t in terms]
    return [t for t in rows if t["term"] and t["pubmed_query"]]


def clean_l3_keywords(keywords) -> list[str]:
    """L3 priority keywords, trimmed, without blanks or repeats (ignoring case)."""
    out = {}
    for k in keywords:
        k = str(k or "").strip()
        if k:
            out.setdefault(k.lower(), k)
    return list(out.values())


def load_settings(path: str) -> dict:
    """Terms, sources and priority keywords saved from the page's Settings dialog (data/settings.json), or {} if never saved."""
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return clean_settings(json.load(f))


def clean_settings(s: dict) -> dict:
    s["terms"] = clean_terms(s.get("terms") or [])
    s["priority_keywords"] = clean_l3_keywords(s.get("priority_keywords") or [])
    return s


def tag_disease(text: str) -> list[str]:
    hits = [name for name, pat in DISEASES.items() if re.search(pat, text, re.I)]
    return hits or ["Other"]


def tag_study_type(pub_types: list[str], text: str) -> str:
    for pt, label in STUDY_TYPES:
        if pt in pub_types:
            return label
    if re.search(r"^\[?retracted", text, re.I):
        return "Retracted"
    if re.search(r"\b(bibliometric|scoping review|narrative review|literature review)\b", text, re.I):
        return "Review"
    if re.search(r"\bcase (report|series)\b", text, re.I):
        return "Case report"
    if re.search(r"\b(real.world|retrospective|cohort|registry|claims|database)\b", text, re.I):
        return "Real-world"
    if re.search(r"\b(survey|questionnaire)\b", text, re.I):
        return "Survey"
    if re.search(r"\bphase (3|III)\b", text, re.I):
        return "Phase 3"
    if re.search(r"\b(in vitro|in vivo|mice|murine|cell line)\b", text, re.I):
        return "Preclinical"
    return "Article"


def tag_intent(title: str, abstract: str | None, study_type: str) -> tuple[str, str]:
    """What the article is about, one of INTENTS, and why: the study type or the words that decided it.
    The title and study type decide (INTENT_RULES, in order); the abstract only settles articles they say
    nothing about, since nearly every abstract mentions both safety and efficacy."""
    for intent, types, pattern, unless in INTENT_RULES:
        if study_type in types:
            return intent, f"study type: {study_type}"
        m = re.search(pattern, title, re.I)
        if m and not (unless and re.search(unless, title, re.I)):
            return intent, f'title: "{m.group(0)}"'
    if study_type in ("Case report", "Letter"):          # a case naming a treatment, else a clinical picture
        if re.search(r"\b(for|with)\b", title, re.I):
            return "Efficacy", f"{study_type.lower()} about a treatment"
        return "Disease state", f"{study_type.lower()} without a treatment in the title"
    for intent, pattern in INTENT_ABSTRACT_RULES:
        m = re.search(pattern, abstract or "", re.I)
        if m:
            return intent, f'abstract: "{m.group(0)}"'
    return "Other", "no rule matched"


def tag_keywords(title: str, abstract: str | None, limit: int = 8) -> list[str]:
    """The KEYWORDS the article is about: those in the title first, then those the abstract mentions at least
    twice (a single mention is often a list of earlier treatments); at most `limit`, in KEYWORDS order. The page
    adds the disease, intent, study type and sponsor tags to them."""
    in_title = [k for k, pat in KEYWORDS.items() if re.search(pat, title, re.I)]
    in_abstract = [k for k, pat in KEYWORDS.items() if k not in in_title and len(re.findall(pat, abstract or "", re.I)) >= 2]
    picked = (in_title + in_abstract)[:limit]
    return [k for k in KEYWORDS if k in picked]


def tag_sponsor(a: dict) -> str:
    text = f"{a.get('coi','')} {' '.join(a.get('grants', []))} {a.get('abstract') or ''}"
    m = re.search(r"(?:funded|supported|sponsored) by ([A-Z][A-Za-z&.\- ]{2,40}?)(?:[,.;]| and | Inc| Ltd| LLC| Pharma)", text)
    if m:
        return re.sub(r"\s+(Corporation|Corp|Inc|Ltd|LLC|Pharmaceuticals?|Biotherapeutics|Company|Co)\.?$", "", m.group(1).strip())
    companies = ["Arcutis", "Incyte", "Dermavant", "Organon", "LEO Pharma", "Eli Lilly", "Lilly", "AbbVie", "Pfizer",
                 "Sanofi", "Regeneron", "Amgen", "Bristol Myers Squibb", "Bristol-Myers Squibb", "Novartis", "UCB", "Galderma", "Union Therapeutics", "Almirall"]
    found = [c for c in companies if re.search(r"\b" + re.escape(c) + r"\b", text)]
    return found[0] if found else "Not stated"


def match_l3(l3_keywords, title: str, abstract: str | None) -> list[str]:
    """The L3 priority keywords in the title or abstract. Each is one phrase; case and punctuation are ignored
    and words match as prefixes, as in query_matches: 'long-term' finds 'Long term', 'child' finds 'children'."""
    text = f"{title} {abstract or ''}"
    return [k for k in l3_keywords if query_matches('"' + k.replace('"', "") + '"', text)]


def apply_rules(rules, title, abstract):
    for r in rules:
        hay = title if r["applies_to"] == "title" else f"{title} {abstract or ''}"
        if re.search(r["pattern"], hay):
            return r["rule_name"]
    return None


def format_note(a: dict) -> str:
    """Why a record may have little or no text. Shown in the summary when there is nothing else to say."""
    notes = []
    if re.search(r"suppl", a.get("issue") or "", re.I) or a.get("study_type") == "Conference abstract":
        notes.append("Published in a supplement (likely a conference abstract); PubMed carries the citation only, and the text sits in the publisher's PDF")
    if a.get("study_type") in ("Letter", "Comment", "Editorial"):
        notes.append(f"{a['study_type']} to the editor: no abstract is published for this format")
    if (a.get("language") or "eng") != "eng":
        notes.append(f"Not in English ({a['language']}); PubMed may hold only a translated title")
    if a.get("record_status") in ("Publisher", "In-Data-Review", "In-Process") and not a.get("abstract"):
        notes.append("Ahead-of-print record; the abstract may be added by PubMed later (re-fetched automatically on later runs)")
    if a.get("study_type") == "Retracted":
        notes.append("This article has been RETRACTED")
    if a.get("study_type") == "Preprint":
        notes.append("Preprint: not peer reviewed")
    if a.get("pmc") and not (a.get("pmc_text") or {}).get("body"):
        notes.append("A PMC record exists but the full text is embargoed or not in the open-access subset, so it could not be read")
    if a.get("origin") == "Google Scholar":
        snip = a.get("scholar_snippet")
        notes.append("Found only by Google Scholar and not matched to a PubMed or OpenAlex record, so there is no abstract"
                     + (f"; Scholar's snippet: “{snip}”" if snip else ""))
    return " ".join(n + "." for n in notes)


def priority(product, study, access, rule, l3=()):
    if rule:
        return "Low"
    if l3:                                             # an L3 priority keyword is in the title or abstract
        return "High"
    if access == "Title only":
        return "Check"
    if study in ("Preclinical",):
        return "Medium" if product == "Roflumilast" else "Low"
    if product == "Roflumilast":
        return "High"
    if product == "Competitor" and study in ("Phase 3", "Phase 2", "RCT", "Real-world", "Meta-analysis"):
        return "High"
    if study in ("Preclinical", "Letter", "Editorial"):
        return "Low"
    return "Medium"


# ----------------------------------------------------------------------------- summaries (no LLM)
def sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+(?=[A-Z0-9])", text or "") if s.strip()]


def summarise(a: dict, access: str) -> tuple[str, list[str], str]:
    """Summary, key points and positioning line built from the structured abstract."""
    secs = dict(a.get("sections") or [])
    ft = a.get("pmc_text") or {}
    if access == "Title only":
        why_none = a.get("format_note") or f"{a.get('origin', 'PubMed')} has no abstract for this record and there is no open-access full text on PMC."
        return (f"Only the title is available. {why_none} Retrieve the article before summarising.",
                [], positioning(a) + " Needs manual retrieval before it can be assessed.")
    if not a.get("abstract") and ft.get("abstract"):
        a["abstract"] = ft["abstract"]
    if not a.get("abstract") and ft.get("body"):
        return summarise_body(a, ft)
    bg = secs.get("Background") or secs.get("Introduction") or secs.get("Objective") or secs.get("Objectives") or ""
    methods = secs.get("Methods") or secs.get("Method") or secs.get("Materials And Methods") or ""
    results = secs.get("Results") or secs.get("Findings") or ""
    concl = secs.get("Conclusions") or secs.get("Conclusion") or secs.get("Interpretation") or ""
    if results or concl:                       # structured abstract
        summary = " ".join(sentences(bg)[:1] + sentences(methods)[:1] + sentences(concl)[:1]).strip()
        points = [s for s in sentences(results) if re.search(r"\d", s)][:4] or sentences(results)[:3]
    else:                                      # unstructured: first two + last sentence
        ss = sentences(a.get("abstract") or "")
        summary = " ".join(ss[:2])
        points = [s for s in ss[2:-1] if re.search(r"\d", s)][:3]
        if ss and len(ss) > 2:
            summary += (" " if summary else "") + ss[-1]
    if access == "Abstract":
        summary = summary + " (Based on the abstract only.)"
        if a.get("format_note"):
            summary += " " + a["format_note"]
    why = positioning(a)
    return summary, points, why


def summarise_body(a: dict, ft: dict) -> tuple[str, list[str], str]:
    """No abstract anywhere: build from the PMC body. First sentences of the opening section, closing
    sentences of the last section, and numeric sentences as key points."""
    secs = ft.get("sections") or []
    opening = sentences(secs[0][1] if secs else ft["body"])
    closing_src = next((t for title, t in reversed(secs) if re.search(r"conclu|discuss|summary", title, re.I)), None)
    closing = sentences(closing_src or (secs[-1][1] if secs else ""))
    summary = " ".join(opening[:2] + closing[-2:]).strip() or " ".join(sentences(ft["body"])[:3])
    summary += " (Summarised from the open-access full text on PMC; no abstract was published.)"
    body_sents = sentences(ft["body"])
    points = [s for s in body_sents if re.search(r"\d+(\.\d+)?\s?(%|weeks?|months?|days?|patients?|years?|mg)", s)][:4]
    return summary, points, positioning(a)


def positioning(a: dict) -> str:
    p, dz, st = a["product"], ", ".join(a["disease"]), a["study_type"]
    if p == "Roflumilast":
        base = f"Own-product evidence ({st.lower()}) in {dz}."
    elif p == "Competitor":
        base = f"Competitor data ({st.lower()}) in {dz}; relevant for positioning conversations."
    else:
        base = f"Class / landscape evidence ({st.lower()}) in {dz}."
    flags = []
    if a["sponsor"] not in ("Not stated",):
        flags.append(f"sponsor: {a['sponsor']}")
    if st in ("Case report",):
        flags.append("single case; treat as anecdotal")
    if "Other" in a["disease"]:
        flags.append("indication outside the core disease set; check for off-label use")
    return base + (" Caveats: " + "; ".join(flags) + "." if flags else "")


# ----------------------------------------------------------------------------- build
def enrich(a: dict, term_cat: dict, rules, term_query: dict | None = None, l3_keywords=()) -> dict:
    text = f"{a['title']} {a.get('abstract') or ''} {' '.join(a.get('mesh') or [])}"
    a["disease"] = tag_disease(text)
    cats = {term_cat.get(t, "Class") for t in a["matched_terms"]}
    a["product"] = "Roflumilast" if "Roflumilast" in cats else "Competitor" if "Competitor" in cats else "Class"
    a["study_type"] = tag_study_type(a.get("pub_types") or [], text)
    a["intent"], a["intent_why"] = tag_intent(a["title"], a.get("abstract"), a["study_type"])
    a["keywords"] = tag_keywords(a["title"], a.get("abstract"))
    a["sponsor"] = tag_sponsor(a)
    ft = a.get("pmc_text") or {}
    if ft.get("body"):
        a["access"] = "Full text"
    elif a.get("abstract") or ft.get("abstract"):
        a["access"] = "Abstract"
    else:
        a["access"] = "Title only"
    a["rule"] = apply_rules(rules, a["title"], a.get("abstract"))
    if a["study_type"] in ("Erratum", "Retraction notice") and not a["rule"]:
        a["rule"] = "Erratum / retraction notice"
    # Google Scholar matches full text, so a Scholar-only hit often just cites the drug in its body or references
    if not a["rule"] and term_query and a.get("sources") == ["Google Scholar"] and not any(
            query_matches(term_query.get(t, t), f"{a['title']} {a.get('abstract') or ''}") for t in a["matched_terms"]):
        a["rule"] = "Term only in full text (Google Scholar)"
    a["format_note"] = format_note(a)
    a["l3_keywords"] = match_l3(l3_keywords, a["title"], a.get("abstract"))
    a["priority"] = priority(a["product"], a["study_type"], a["access"], a["rule"], a["l3_keywords"])
    a["summary"], a["points"], a["why"] = summarise(a, a["access"])
    return a


def page_status(rule: str | None, decision: str | None) -> str:
    """The feed an article is on: 'review', 'accepted' or 'ignored'. A reviewer's decision wins over the
    exclusion rules; 'restored' sends an article a rule ignored back to review. Same logic as statusOf() in the page."""
    if decision == "accepted":
        return "accepted"
    if decision in ("ignored", "removed"):           # 'removed' is the older name for 'ignored'
        return "ignored"
    return "ignored" if rule and decision != "restored" else "review"


def split_reasons(text: str | None) -> list[str]:
    """The reasons column of decisions.csv: the keywords and reasons a reviewer ticked, separated by ';'."""
    return [r.strip() for r in (text or "").split(";") if r.strip()]


def to_page_row(a: dict, decision: str | None, key: str | None = None, reasons=()) -> dict:
    decision = "ignored" if decision == "removed" else decision or None
    note = f"Matched rule: {a['rule']}" if a["rule"] else ""
    first = (a["authors"][0] if a["authors"] else "Unknown") + (" et al." if len(a["authors"]) > 1 else "")
    year = a["pub_date"][:4]
    origin = a.get("origin", "PubMed")
    via = {"PubMed": "PubMed" + (" (PMC full text)" if a.get("pmc") else ""), "OpenAlex": "OpenAlex",
           "Google Scholar": a.get("scholar_via") or "Google Scholar"}[origin]
    links = article_links(a)
    return dict(
        id=key or a["pmid"], title=a["title"],
        cite=f"{', '.join(a['authors'][:3])}{' et al.' if len(a['authors']) > 3 else ''} · {a['journal']} · {year}",
        meta=f"{first} · {a['journal_abbrev'] or a['journal']} {year} · {a['study_type']}",
        src=", ".join(a.get("sources") or [origin]), via=via,
        term=", ".join(a["matched_terms"]), dz=a["disease"], product=a["product"], type=a["study_type"],
        intent=a["intent"], intent_why=a["intent_why"], access=a["access"], prio=a["priority"], l3=a["l3_keywords"], sponsor=a["sponsor"],
        link=links[0]["url"] if links else "", links=links,
        summary=a["summary"], points=a["points"], why=a["why"], status=page_status(a["rule"], decision),
        rule=a["rule"], note=note, pub_date=a["pub_date"], decision=decision, keywords=a.get("keywords") or [],
        reasons=list(reasons) if decision in ("accepted", "ignored") else [],
    )


def article_links(a: dict) -> list[dict]:
    """Best link first: PMC full text, the publisher page via DOI, the PubMed record, then any other
    open-access copy and the page Google Scholar pointed to."""
    out = []
    if a.get("pmc"):
        out.append({"label": "PMC full text", "url": f"https://pmc.ncbi.nlm.nih.gov/articles/{a['pmc']}/"})
    if a.get("doi"):
        out.append({"label": "Publisher (DOI)", "url": f"https://doi.org/{a['doi']}"})
    if a.get("pmid"):
        out.append({"label": "PubMed record", "url": f"https://pubmed.ncbi.nlm.nih.gov/{a['pmid']}/"})
    if a.get("oa_url"):
        out.append({"label": "Open-access copy", "url": a["oa_url"]})
    if a.get("scholar_link"):
        out.append({"label": "Found by Google Scholar", "url": a["scholar_link"]})
    if not out and a.get("url"):
        out.append({"label": "Open article", "url": a["url"]})
    unique = {}
    for link in out:                                   # the same URL can come from two sources; keep the first label
        unique.setdefault(link["url"], link)
    return list(unique.values())


def decision_for(key: str, a: dict, decisions: dict) -> str | None:
    """A reviewer decision stays with the article when its key changes (e.g. a Scholar hit later found in PubMed)."""
    return next((decisions[k] for k in (key, *a.get("aliases", [])) if k in decisions), None)


def render(rows: list[dict], template_path: str, out_path: str, header_line: str, settings: dict | None = None):
    """Writes the page to a file (without --tables; with them, the app builds it on every request)."""
    with open(template_path, encoding="utf-8") as f:
        html = fill_page(f.read(), rows, header_line, settings)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)


# ----------------------------------------------------------------------------- Unity Catalog tables
def timestamp(s: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(s) if s else None
    except ValueError:
        return None


def article_row(key: str, a: dict, position: int | None = None) -> dict:
    """A row of the articles table: the fields worth querying in SQL, the whole record as JSON, and, for an article
    on the page, its place there and its page row (without a decision: the app adds the current one)."""
    try:
        pub_date = date.fromisoformat(a.get("pub_date") or "")
    except ValueError:
        pub_date = None
    return dict(
        id=key, aliases=a.get("aliases") or [], pmid=a.get("pmid"), doi=a.get("doi"), pmc=a.get("pmc"), title=a.get("title"),
        authors=a.get("authors") or [], journal=a.get("journal"), pub_date=pub_date, first_seen=timestamp(a.get("first_seen")),
        sources=a.get("sources") or [], matched_terms=a.get("matched_terms") or [], disease=a.get("disease") or [],
        product=a.get("product"), study_type=a.get("study_type"), intent=a.get("intent"), access=a.get("access"),
        priority=a.get("priority"), l3_keywords=a.get("l3_keywords") or [], exclusion_rule=a.get("rule"), sponsor=a.get("sponsor"),
        abstract=a.get("abstract"), summary=a.get("summary"), url=a.get("url"), record=json.dumps(a, ensure_ascii=False),
        page_position=position,
        page_row=None if position is None else json.dumps(to_page_row(a, None, key=key), ensure_ascii=False),
    )


def decision_row(r: dict) -> dict:
    """A decisions.csv row as a row of the decisions table."""
    return dict(article_id=r["pmid"], decision="ignored" if r["decision"] == "removed" else r["decision"],
                reasons=split_reasons(r.get("reasons")), decided_by=r.get("by") or None, decided_at=timestamp(r.get("at")))


def settings_row(s: dict) -> dict:
    """Saved settings (as load_settings returns them) as a row of the settings table."""
    return dict(updated_at=timestamp(s.get("updated_at")) or datetime.now(timezone.utc), updated_by=s.get("updated_by") or None,
                sources=s.get("sources") or [], scholar_pages=int(s.get("scholar_pages") or 1),
                priority_keywords=s.get("priority_keywords") or [], terms=[tuple(t[k] for k in TERM_FIELDS) for t in s.get("terms") or []])


def run_row(settings: dict, since: date, until: date, terms_from: str, articles: int, new_articles: int, shown: int) -> dict:
    """This run as a row of the runs table: what it searched with (the page's SETTINGS) and the articles it ended with."""
    keys = settings["keys"]
    return dict(run_at=datetime.now(timezone.utc), window_start=since, window_end=until, sources=settings["sources"],
                scholar_pages=settings["scholar_pages"], priority_keywords=settings["priority_keywords"],
                terms=[tuple(t[k] for k in TERM_FIELDS) for t in settings["terms"]], terms_from=terms_from,
                api_keys=(keys["pubmed"], keys["openalex"], keys["scholar"]), articles=articles, new_articles=new_articles, shown=shown)


class Tables:
    """The article store, reviewer decisions, saved settings and run log as the Delta tables <prefix>_articles,
    _decisions, _settings and _runs (tables.sql), through Spark. The job writes _articles and _runs; the app writes
    _decisions and _settings, and builds the page from all four."""
    def __init__(self, prefix: str):
        from pyspark.sql import SparkSession
        self.spark = SparkSession.builder.getOrCreate()
        self.articles_t, self.decisions_t, self.settings_t, self.runs_t = (
            f"{prefix}_{n}" for n in ("articles", "decisions", "settings", "runs"))
        for t in (self.articles_t, self.decisions_t, self.settings_t, self.runs_t):
            if not self.spark.catalog.tableExists(t):
                sys.exit(f"Table {t} does not exist or you can't read it. Create the tables with tables.sql (DEPLOY.md, Step 3).")

    def _write(self, table: str, rows: list[dict], overwrite=False):
        schema = self.spark.table(table).schema          # tables.sql is the one place the columns are defined
        df = self.spark.createDataFrame([tuple(r[f.name] for f in schema.fields) for r in rows], schema)
        df.write.insertInto(table, overwrite=overwrite)

    def articles(self) -> dict:
        return {r["id"]: json.loads(r["record"]) for r in self.spark.table(self.articles_t).select("id", "record").collect()}

    def save_articles(self, store: dict, positions: dict | None = None):
        """Replaces the articles table with the store; `positions` ({id: place}) are the articles on the page."""
        positions = positions or {}
        self._write(self.articles_t, [article_row(k, a, positions.get(k)) for k, a in store.items()], overwrite=True)

    def log_run(self, row: dict):
        self._write(self.runs_t, [row])

    def decisions(self) -> dict:
        """{article id: {decision, reasons}}, like the rows of decisions.csv."""
        return {r["article_id"]: {"decision": r["decision"], "reasons": "; ".join(r["reasons"] or [])}
                for r in self.spark.table(self.decisions_t).collect()}

    def settings(self) -> dict:
        rows = self.spark.table(self.settings_t).orderBy("updated_at", ascending=False).limit(1).collect()
        return clean_settings(rows[0].asDict(recursive=True)) if rows else {}

    def import_files(self, data_dir: str):
        """Copies in the articles.json, decisions.csv and settings.json that earlier versions kept in data_dir, then
        renames each to *.imported so it is imported only once. What the tables already hold wins: articles a run
        has written, or a decision saved in the app since. If settings were never saved, terms.csv becomes the first."""
        paths = {n: os.path.join(data_dir, n) for n in ("articles.json", "decisions.csv", "settings.json")}
        if os.path.exists(paths["articles.json"]):
            if self.spark.table(self.articles_t).limit(1).count():
                print(f"{paths['articles.json']} not imported: {self.articles_t} already holds articles")
            else:
                with open(paths["articles.json"], encoding="utf-8") as f:
                    store = json.load(f)
                self.save_articles(store)
                print(f"imported {len(store)} articles from {paths['articles.json']}")
        if os.path.exists(paths["decisions.csv"]):
            have = self.decisions()
            rows = [decision_row(r) for r in read_csv(paths["decisions.csv"]) if r.get("decision") and r["pmid"] not in have]
            if rows:
                self._write(self.decisions_t, rows)
            print(f"imported {len(rows)} decisions from {paths['decisions.csv']}")
        if os.path.exists(paths["settings.json"]):
            self._write(self.settings_t, [settings_row(load_settings(paths["settings.json"]))])
            print(f"imported settings from {paths['settings.json']}")
        if not self.settings():
            terms_csv = os.path.join(HERE, "terms.csv")
            self._write(self.settings_t, [settings_row(dict(terms=clean_terms(read_csv(terms_csv)), sources=SOURCE_IDS, updated_by="terms.csv"))])
            print(f"saved the terms in {terms_csv} as the first settings")
        for p in paths.values():
            if os.path.exists(p):
                os.rename(p, p + ".imported")


def secret(env: str, scope: str | None, key: str) -> str | None:
    """An API key from the environment or, when running on Databricks, from a secret scope."""
    if os.environ.get(env):
        return os.environ[env]
    if scope:
        try:
            from databricks.sdk.runtime import dbutils
            return dbutils.secrets.get(scope=scope, key=key)
        except Exception:
            return None
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--api-key", help="NCBI API key (or env PUBMED_API_KEY)")
    ap.add_argument("--serpapi-key", help="SerpApi key for Google Scholar (or env SERPAPI_KEY); Scholar is skipped without one")
    ap.add_argument("--openalex-key", help="OpenAlex API key (or env OPENALEX_API_KEY); optional")
    ap.add_argument("--secret-scope", help="Databricks secret scope holding pubmed-api-key, serpapi-key, openalex-api-key")
    ap.add_argument("--email", default=os.environ.get("CONTACT_EMAIL"), help="contact email for the User-Agent")
    ap.add_argument("--sources", help="comma-separated: pubmed, openalex, scholar (default: as saved in Settings, else all three)")
    ap.add_argument("--scholar-pages", type=int, help="pages of 20 Google Scholar results per term, one SerpApi search each "
                                                      "(default: as saved in Settings, else 1)")
    ap.add_argument("--days", type=int, default=90, help="look-back window in days (default 90, the page's longest Published filter)")
    ap.add_argument("--since", help="start date yyyy-mm-dd (overrides --days)")
    ap.add_argument("--until", help="end date yyyy-mm-dd (default today)")
    ap.add_argument("--terms", help="terms CSV (default: the terms saved in Settings, else terms.csv)")
    ap.add_argument("--exclusions", default=os.path.join(HERE, "exclusions.csv"))
    ap.add_argument("--template", default=TEMPLATE)
    ap.add_argument("--out", default=os.path.join(HERE, "literature_update.html"), help="the page file (not written with --tables)")
    ap.add_argument("--data-dir", default=os.environ.get("LITSURV_DATA_DIR") or os.path.join(HERE, "data"),
                    help="folder holding articles.json and decisions.csv (default ./data); with --tables, only read once to import them")
    ap.add_argument("--tables", help="Unity Catalog table name prefix, e.g. medical_prod.apps.literature_surveillance: keep the store, "
                                     "decisions, settings and a run log in its _articles, _decisions, _settings and _runs tables "
                                     "(tables.sql), from which the app builds the page; needs Spark")
    ap.add_argument("--show-days", type=int, default=90, help="how far back the page shows (default 90)")
    ap.add_argument("--rebuild", action="store_true", help="re-fetch every stored PMID (use after a parser fix)")
    ap.add_argument("--no-fetch", action="store_true", help="skip searching; re-tag and re-render the local store only")
    args = ap.parse_args()

    started = now()
    store_path = os.path.join(args.data_dir, "articles.json")
    decisions_path = os.path.join(args.data_dir, "decisions.csv")
    settings_path = os.path.join(args.data_dir, "settings.json")
    if not args.tables:
        os.makedirs(args.data_dir, exist_ok=True)
    args.api_key = args.api_key or secret("PUBMED_API_KEY", args.secret_scope, "pubmed-api-key")
    serpapi_key = args.serpapi_key or secret("SERPAPI_KEY", args.secret_scope, "serpapi-key")
    openalex_key = args.openalex_key or secret("OPENALEX_API_KEY", args.secret_scope, "openalex-api-key")
    if args.api_key:
        m = re.search(r"[0-9a-f]{36}", args.api_key, re.I)
        if not m:
            sys.exit("PUBMED_API_KEY does not look like an NCBI key (36 hex characters). Check for extra text around it.")
        if m.group(0) != args.api_key.strip():
            print("note: extra text around the API key was ignored", file=sys.stderr)
        args.api_key = m.group(0)
    db = Tables(args.tables) if args.tables else None
    if db:
        db.import_files(args.data_dir)
    saved = db.settings() if db else load_settings(settings_path)   # command-line options win over saved settings
    sources = {s.strip().lower() for s in (args.sources.split(",") if args.sources else saved.get("sources") or SOURCE_IDS) if s.strip()}
    scholar_pages = args.scholar_pages or saved.get("scholar_pages") or 1
    until = date.fromisoformat(args.until) if args.until else date.today()
    since = date.fromisoformat(args.since) if args.since else until - timedelta(days=args.days)
    if saved.get("terms") and not args.terms:
        terms_from, terms = db.settings_t if db else settings_path, saved["terms"]
    else:
        terms_from = args.terms or os.path.join(HERE, "terms.csv")
        terms = clean_terms(read_csv(terms_from))
    l3_keywords = saved.get("priority_keywords") or []
    print(f"{len(terms)} terms from {terms_from} · {len(l3_keywords)} L3 priority keywords · "
          f"sources: {', '.join(s for s in SOURCE_IDS if s in sources)}")
    rules = read_csv(args.exclusions)
    term_cat = {t["term"]: t["product_category"] for t in terms}
    # OpenAlex / Scholar queries default to the PubMed query; optional terms.csv columns override them
    term_query = {t["term"]: t.get("openalex_query") or plain_query(t["pubmed_query"]) for t in terms}
    if db:
        store: dict[str, dict] = db.articles()
    else:
        store = json.load(open(store_path, encoding="utf-8")) if os.path.exists(store_path) else {}
    for a in store.values():                           # records written before OpenAlex and Scholar were added
        a.setdefault("origin", "PubMed")
        a.setdefault("sources", ["PubMed"])
    idx = Index(store)

    # 1. search and fetch -----------------------------------------------------
    if not args.no_fetch:
        pm = PubMed(args.api_key, args.email)
        oa = OpenAlex(args.email, openalex_key) if "openalex" in sources else None
        gs = Scholar(serpapi_key) if "scholar" in sources and serpapi_key else None
        if "scholar" in sources and not gs:
            print("Google Scholar skipped: no SerpApi key (--serpapi-key, env SERPAPI_KEY or secret serpapi-key)", file=sys.stderr)
        hits: dict[str, dict] = {}            # PMID → {terms, feeds}: PubMed hits, plus OpenAlex hits that carry a PMID
        oa_by_pmid, oa_only = {}, []          # OpenAlex copies, in case PubMed cannot serve the PMID; records with no PMID
        for t in terms:
            counts = []
            if "pubmed" in sources:
                try:
                    ids = pm.search(t["pubmed_query"], since, until)
                    for pmid in ids:
                        note_hit(hits, pmid, t["term"], "PubMed")
                    counts.append(f"PubMed {len(ids):4d}")
                except Exception as e:
                    counts.append(f"PubMed ERROR {e}")
            if oa:
                q = term_query[t["term"]]
                try:
                    recs = [r for r in oa.search(q, since, until) if query_matches(q, f"{r['title']} {r.get('abstract') or ''}")]
                    for r in recs:
                        r.update(matched_terms=[t["term"]], sources=["OpenAlex"])
                        if r["pmid"]:
                            note_hit(hits, r["pmid"], t["term"], "OpenAlex")
                            oa_by_pmid.setdefault(r["pmid"], r)
                        else:
                            oa_only.append(r)
                    counts.append(f"OpenAlex {len(recs):4d}")
                except Exception as e:
                    counts.append(f"OpenAlex ERROR {e}")
            print(f"  {t['term']:36s} " + " · ".join(counts))

        # records fetched while still incomplete (no abstract, or PMC not yet readable) are re-fetched
        # for up to 120 days so ahead-of-print abstracts and embargo releases are picked up
        cutoff_refresh = (datetime.now() - timedelta(days=120)).isoformat(timespec="seconds")
        pubmed_keys = [p for p, a in store.items() if a["origin"] == "PubMed"]
        refresh = pubmed_keys if args.rebuild else [
            p for p in pubmed_keys if (store[p].get("first_seen") or "") >= cutoff_refresh
            and (not store[p].get("abstract") or (store[p].get("pmc") and not (store[p].get("pmc_text") or {}).get("body")))]
        new_ids, missing = ingest_pubmed(pm, store, idx, hits, refresh, args.rebuild)
        print(f"{len(hits)} PMIDs matched, {len(new_ids)} new, {len(refresh)} {'stored' if args.rebuild else 'incomplete'} records re-checked")
        if missing:
            print(f"  not returned by PubMed (listed in search but record not yet available): {', '.join(missing)}")
        for pmid in missing:                          # OpenAlex has it: keep that copy until PubMed catches up
            if pmid in oa_by_pmid:
                merge(store, idx, dict(oa_by_pmid[pmid], matched_terms=sorted(hits[pmid]["terms"]), sources=sort_sources(hits[pmid]["feeds"])))
        if oa:
            n0 = len(store)
            for r in oa_only:
                if r.get("pmc") and idx.find(r) is None:
                    r["pmc_text"] = pm.fetch_pmc(r["pmc"])
                merge(store, idx, r)
            print(f"OpenAlex: {len(oa_only)} hits without a PMID, {len(store) - n0} of them new articles")

        if gs:
            via_scholar, scholar_hit, oa_match = {}, {}, {}   # PMIDs OpenAlex found for Scholar hits; those hits; lookups by title
            n0 = len(store)
            for t in terms:
                try:
                    recs = gs.search(t.get("scholar_query") or scholar_query(term_query[t["term"]]), since, until, scholar_pages)
                except Exception as e:
                    print(f"  {t['term']:36s} Google Scholar ERROR {e}", file=sys.stderr)
                    continue
                print(f"  {t['term']:36s} Google Scholar {len(recs):4d}")
                for r in recs:
                    r.update(matched_terms=[t["term"]], sources=["Google Scholar"])
                    if idx.find(r) is not None:               # already stored: just note the feed and term
                        merge(store, idx, r)
                        continue
                    tk = title_key(r["title"])
                    if tk not in oa_match:
                        oa_match[tk] = oa.resolve(r) if oa else None
                    found, scholar = oa_match[tk], {f: r[f] for f in SCHOLAR_FIELDS}
                    if found and found["pmid"]:                   # fetched from PubMed below
                        note_hit(via_scholar, found["pmid"], t["term"], "Google Scholar")
                        scholar_hit.setdefault(found["pmid"], (found, scholar))
                    else:
                        merge(store, idx, dict(found or r, **scholar, matched_terms=[t["term"]], sources=["Google Scholar"]))
            ingest_pubmed(pm, store, idx, via_scholar)
            for pmid, (found, scholar) in scholar_hit.items():   # adds the Scholar link (or the OpenAlex copy if PubMed had none)
                h = via_scholar[pmid]
                merge(store, idx, dict(found, **scholar, matched_terms=sorted(h["terms"]), sources=sort_sources(h["feeds"])))
            print(f"Google Scholar: {len(store) - n0} articles not already found by PubMed or OpenAlex")

    # 2. tag + summarise (always, so edits to terms/rules apply to old rows) ----
    for a in store.values():
        enrich(a, term_cat, rules, term_query, l3_keywords)
    if not db:                                         # with the tables, the store is saved below with the page order
        json.dump(store, open(store_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    # 3. page -----------------------------------------------------------------
    if db:
        decisions = db.decisions()
    else:
        decisions = {r["pmid"]: r for r in read_csv(decisions_path) if r.get("decision")} if os.path.exists(decisions_path) else {}
    cutoff = (until - timedelta(days=args.show_days)).isoformat()

    def page_row(k, a):
        d = decision_for(k, a, decisions) or {}
        return to_page_row(a, d.get("decision"), key=k, reasons=split_reasons(d.get("reasons")))
    rows = [page_row(k, a) for k, a in store.items()
            if (a.get("first_seen") or "")[:10] >= cutoff or (a.get("pub_date") or "") >= cutoff]
    order = {"High": 0, "Medium": 1, "Check": 2, "Low": 3}
    rows.sort(key=lambda r: (order.get(r["prio"], 9), r["pub_date"]), reverse=False)
    rows.sort(key=lambda r: (order.get(r["prio"], 9), not r["l3"]))     # L3 keyword matches first within each priority
    page_settings = dict(terms=terms, sources=[s for s in SOURCE_IDS if s in sources], scholar_pages=scholar_pages,
                         priority_keywords=l3_keywords,
                         keys=dict(pubmed=bool(args.api_key), openalex=bool(openalex_key), scholar=bool(serpapi_key)))
    if db:                                             # the app builds the page from these two tables on every request
        db.save_articles(store, {r["id"]: i for i, r in enumerate(rows)})
        new = sum((a.get("first_seen") or "") >= started for a in store.values())
        db.log_run(run_row(page_settings, since, until, terms_from, len(store), new, len(rows)))
        page_to = f"{db.articles_t}, {db.runs_t}"
    else:
        render(rows, args.template, args.out, page_header(sources, since, until, datetime.now()), page_settings)
        page_to = args.out
    feeds = {k: sum(r["status"] == k for r in rows) for k in ("review", "accepted", "ignored")}
    acc = {k: sum(r["access"] == k for r in rows) for k in ("Full text", "Abstract", "Title only")}
    pri = {k: sum(r["prio"] == k for r in rows if r["status"] == "review") for k in ("High", "Medium", "Check", "Low")}
    src = {k: sum(k in r["src"] for r in rows) for k in SOURCES}
    print(f"store: {len(store)} articles · page: {len(rows)} shown: {feeds['review']} to review, {feeds['accepted']} accepted, "
          f"{feeds['ignored']} ignored → {page_to}")
    print(f"sources: {src['PubMed']} PubMed · {src['OpenAlex']} OpenAlex · {src['Google Scholar']} Google Scholar · "
          f"{sum(', ' in r['src'] for r in rows)} found by more than one (shown once)")
    print(f"access: {acc['Full text']} full text (PMC) · {acc['Abstract']} abstract · {acc['Title only']} title only")
    print(f"priority (to review): {pri['High']} high · {pri['Medium']} medium · {pri['Check']} check · {pri['Low']} low · "
          f"{sum(bool(r['l3']) for r in rows if r['status'] == 'review')} with an L3 keyword")
    intents = {k: sum(r["intent"] == k for r in rows if r["status"] == "review") for k in INTENTS}
    print("intent (to review): " + " · ".join(f"{n} {k.lower()}" for k, n in intents.items() if n))


if __name__ == "__main__":
    main()
