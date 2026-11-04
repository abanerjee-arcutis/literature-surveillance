"""Offline test: parse a PubMed efetch fixture, tag, summarise, render. `python tests/test_offline.py`."""
import json, os, re, sys, tempfile
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import surveillance as S

arts = S.parse_pubmed_xml(open(os.path.join(HERE, "fixture_efetch.xml"), "rb").read())
assert len(arts) == 8, len(arts)
terms = S.read_csv(os.path.join(ROOT, "terms.csv")); rules = S.read_csv(os.path.join(ROOT, "exclusions.csv"))
term_cat = {t["term"]: t["product_category"] for t in terms}
matched = {"42728891": ["Roflumilast cream"], "42700001": ["Ruxolitinib topical", "Ruxolitinib"],
           "42693807": ["Tapinarof"], "42600000": ["Ruxolitinib"], "42657782": ["Topical steroid withdrawal"],
           "42600001": ["Ruxolitinib topical"], "42600002": ["Topical roflumilast"], "42600003": ["Topical corticosteroids"]}
for a in arts:
    a["matched_terms"] = matched[a["pmid"]]; a["first_seen"] = "2026-09-24T10:00:00"
    if a["pmid"] == "42700001":   # has a PMC id: pretend the PMC full text was fetched
        a["pmc_text"] = {"abstract": None, "body": "Full body text of the Adelphi analysis. " * 20, "sections": []}
    S.enrich(a, term_cat, rules)

by = {a["pmid"]: a for a in arts}
a = by["42728891"]
assert a["product"] == "Roflumilast" and a["disease"] == ["Atopic Dermatitis"] and a["study_type"] == "Case report"
assert a["access"] == "Abstract" and a["priority"] == "Must read" and a["rule"] is None
assert "Based on the abstract only" in a["summary"]
assert a["pmc"] is None and a["doi"] == "10.1089/derm.2026.0001", "reference-list IDs must not leak"
assert "\xa0" not in a["abstract"] and "&#160" not in a["abstract"]

b = by["42700001"]
assert b["access"] == "Full text" and b["study_type"] == "Real-world" and b["sponsor"] == "Incyte"
assert b["priority"] == "Must read" and any("49%" in p for p in b["points"]), b["points"]
assert "Adelphi" in b["summary"] and "comparable" in b["summary"]

c = by["42693807"]
assert c["access"] == "Title only" and c["priority"] == "Of interest" and c["points"] == []
assert c["pub_date"] == "2026-06-01", c["pub_date"]

d = by["42600000"]
assert d["rule"] == "Transplant / haematology context" and d["priority"] == "Low"

# --- edge formats ---------------------------------------------------------------
f = by["42657782"]   # supplement / conference abstract, no abstract text
assert f["access"] == "Title only" and "supplement" in f["summary"].lower(), f["summary"]
g = by["42600001"]   # retraction notice
assert g["study_type"] == "Retraction notice" and g["rule"] == "Erratum / retraction notice" and g["priority"] == "Low"
h = by["42600002"]   # German, ahead of print, no abstract
assert "Not in English" in h["summary"] and "Ahead-of-print" in h["summary"], h["summary"]
k = by["42600003"]   # Bookshelf chapter
assert k["study_type"] == "Book chapter" and k["access"] == "Abstract" and k["journal"] == "StatPearls"

# --- intent ------------------------------------------------------------------------
assert (a["intent"], a["intent_why"]) == ("Safety", 'title: "Allergic"'), a["intent_why"]
assert b["intent"] == "Real-world use" and d["intent"] == "Efficacy" and f["intent"] == "Safety"
assert by["42600002"]["intent"] == "Review & guidance" and g["intent"] == "Other"
for title, study, want in [
    ("Efficacy and safety of tapinarof cream in Japanese children: a phase 3 trial", "Article", "Efficacy"),   # not Safety
    ("Tapinarof-Induced Folliculitis With Pathologic Analysis.", "Letter", "Safety"),
    ("Dupilumab-Induced Vitiligo Managed Effectively With Nemolizumab.", "Case report", "Efficacy"),        # treating the event
    ("Topical ronomilast ameliorates imiquimod-induced psoriasis in mice.", "Preclinical", "Mechanism"),
    ("Roflumilast Cream for Residual Disease on Dupilumab: A Retrospective Chart Review Study.", "Case report", "Real-world use"),
    ("Psychiatric comorbidities in seborrheic dermatitis: a cross-sectional EHR analysis", "Article", "Disease state"),
    ("Patient experience and preferences for treatments in atopic dermatitis", "Survey", "Patient perspective"),
    ("Delphi Consensus Statement on Treatment of Severe Alopecia Areata.", "Article", "Review & guidance"),
    ("Post-scabies Dermatitis Mimicking Treatment Failure: A Diagnostic Challenge.", "Case report", "Disease state"),
    ("Acupuncture for nummular eczema of the lower legs: a case report.", "Case report", "Efficacy"),
]:
    assert S.tag_intent(title, None, study)[0] == want, (title, S.tag_intent(title, None, study))
assert S.tag_intent("Koebner phenomenon in a flap", "A randomized, double-blind study of 40 patients.", "Article") == ("Efficacy", 'abstract: "randomized"')

# --- L3 priority: the L3 reasons and push-down rules score each article; the score sets its priority ----------
assert len(S.L3_INTENTS) == 23 and len(S.L3_PUSH_DOWN) == 9
assert all(list(r) == list(S.L3_RULE_FIELDS) for r in S.DEFAULT_L3_RULES)
assert [h["id"] for h in a["l3_intents"]] == ["own_product", "own_product_mention", "formulation"] and a["l3_score"] == 110
assert a["l3_intents"][0]["keywords"] == ["roflumilast"] and a["auto_accept"] == "Must read: Own product: topical roflumilast"
assert [h["id"] for h in b["l3_intents"]][0] == "comparative" and b["l3_score"] == 95      # 85, +5 for each of two more
assert (c["l3_score"], d["l3_score"], d["priority"], d["auto_accept"]) == (75, 0, "Low", None)
assert [p["id"] for p in d["l3_push_down"]] == ["oral_jak_other"]
assert [S.l3_band(n) for n in (100, 85, 84, 60, 59, 40, 39, 0)] == ["Must read"] * 2 + ["Of interest"] * 2 + ["FYI"] * 2 + ["Low"] * 2
score = lambda title, abstract=None, study=None, text="": S.l3_score(S.DEFAULT_L3_RULES, title, abstract, study, text)
assert score("Roflumilast cream 0.3% for Darier disease: a case report")[0] == 100
assert score("Roflumilast in COPD exacerbations")[0] == 0                                     # none_of: not the cream
assert score("P1234 Roflumilast cream in adolescents")[0] == 60                               # congress abstract: -40
s, _, pushed = score("Roflumilast cream in adults with psoriasis", "A pooled analysis.", text="Funded by Arcutis Biotherapeutics")
assert s == 105 - 35 and [p["id"] for p in pushed] == ["arcutis_funded"], pushed
assert score("Topical roflumilast: a Delphi consensus", text="Funded by Arcutis")[2] == []    # sponsored consensus is not pushed down
assert score("Ruxolitinib cream for vitiligo")[0] == 0 and score("Roflumilast cream for vitiligo")[0] == 100
assert score("Seborrheic dermatitis and quality of life")[1][0]["id"] == "seb_derm"
# L3 keywords saved in Settings count as one more reason, worth OWN_KEYWORD_POINTS; an exclusion rule still makes it Low
assert S.match_l3(["child", "long-term safety", "foam", 'scalp "psoriasis"'], "Long term safety of roflumilast in children", None) == ["child", "long-term safety"]
assert S.match_l3(["vehicle"], "Tapinarof cream", "Compared with vehicle at week 8.") == ["vehicle"]
assert c["l3_keywords"] == [] and d["l3_keywords"] == []
c3 = S.enrich(dict(c), term_cat, rules, l3_keywords=["phototherapy", "scalp"])
d3 = S.enrich(dict(d), term_cat, rules, l3_keywords=["transplant"])
assert c3["l3_keywords"] == ["phototherapy"] and c3["l3_score"] == c["l3_score"] + 5 and c3["l3_intents"][1]["id"] == "own_keywords"
assert S.l3_score(S.DEFAULT_L3_RULES, "Atopic dermatitis registry", None, own=["registry"])[0] == S.OWN_KEYWORD_POINTS
assert d3["l3_keywords"] == ["transplant"] and d3["priority"] == "Low"
assert S.to_page_row(c3, None)["l3"] == ["phototherapy"]
# the _intents table can replace the built-in reasons: a single reason, no push-down rules
only = [dict(intent_id="x", kind="intent", intent="Graft", weight=90, keyword_groups=[dict(field="title", keywords=["graft"])])]
d4 = S.enrich(dict(d), term_cat, rules, l3_rules=only)
assert d4["l3_score"] == 90 and d4["l3_push_down"] == [] and d4["priority"] == "Low"          # the exclusion rule still wins

# --- PMC full text for an article with no abstract ---------------------------
ft = S.parse_pmc_xml(open(os.path.join(HERE, "fixture_pmc.xml"), "rb").read())
assert ft["abstract"] is None and "54-year-old" in ft["body"] and "unsatisfactory 1" not in ft["body"] and "unsatisfactory and relapses" in ft["body"] and ft["sections"][0][0] == "Introduction"
e = dict(pmid="42381826", title="Tapinarof cream 1% for corticosteroid-refractory erythema annulare centrifugum: A first reported case.",
         abstract=None, sections=[], authors=["Malick H"], journal="JAAD case reports", journal_abbrev="JAAD Case Rep",
         pub_date="2026-08-10", doi="10.1016/j.jdcr.2026.07.001", pmc="PMC9999999", pub_types=["Case Reports"], grants=[], coi="",
         url="https://pubmed.ncbi.nlm.nih.gov/42381826/", mesh=[], matched_terms=["Tapinarof"], first_seen="2026-09-24T10:00:00", pmc_text=ft)
S.enrich(e, term_cat, rules)
assert e["access"] == "Full text" and e["summary"] and "PMC" in e["summary"], e["summary"]
assert any("70%" in p or "12 weeks" in p for p in e["points"]), e["points"]
lk = S.article_links(e)
assert lk[0]["url"] == "https://pmc.ncbi.nlm.nih.gov/articles/PMC9999999/" and lk[1]["url"].startswith("https://doi.org/") and lk[2]["url"].endswith("/42381826/")
arts.append(e)

# --- queries for OpenAlex / Google Scholar ---------------------------------------
q = S.plain_query("topical[tiab] AND (JAK inhibitor*[tiab] OR Janus kinase inhibitor*[tiab])")
assert q == 'topical AND ("JAK inhibitor" OR "Janus kinase inhibitor")', q
assert S.plain_query("ruxolitinib[tiab] AND (dermatitis[tiab] OR vitiligo[tiab])") == "ruxolitinib AND (dermatitis OR vitiligo)"
assert S.scholar_query(q) == 'topical ("JAK inhibitor" OR "Janus kinase inhibitor")', S.scholar_query(q)
assert S.query_matches(q, "Topical JAK inhibitors in vitiligo") and not S.query_matches(q, "Oral JAK inhibitors in vitiligo")
assert not S.query_matches("roflumilast AND cream", "Roflumilast foam for scalp psoriasis")
assert S.query_matches('"topical steroid withdrawal"', "Topical-steroid withdrawal: a review")

# --- OpenAlex and Google Scholar records ------------------------------------------
o = S.parse_openalex_work({
    "id": "https://openalex.org/W4400000001", "doi": "https://doi.org/10.1080/09546634.2026.2698965",
    "display_name": "Real-world clinical experience with <i>ruxolitinib</i> cream versus systemic therapies in moderate atopic dermatitis",
    "publication_date": "2026-08-01", "type": "article", "language": "en", "ids": {"pmid": "https://pubmed.ncbi.nlm.nih.gov/42700001"},
    "authorships": [{"author": {"display_name": "Jane A. Liu"}}], "primary_location": {"source": {"display_name": "J Dermatolog Treat"}},
    "abstract_inverted_index": {"Adelphi": [0], "analysis": [1], "of": [2], "AD": [3]}, "grants": [{"funder_display_name": "Incyte"}]})
assert o["pmid"] == "42700001" and o["doi"] == "10.1080/09546634.2026.2698965" and o["openalex_id"] == "W4400000001"
assert o["abstract"] == "Adelphi analysis of AD" and o["authors"] == ["Liu JA"] and o["grants"] == ["Incyte"] and "<i>" not in o["title"]

g = S.parse_scholar_result({
    "title": "[HTML] Efficacy of roflumilast cream 0.15% in children with atopic dermatitis …", "result_id": "abc123",
    "link": "https://www.frontierspartnerships.org/journals/jcia/articles/10.3389/jcia.2026.17307/full",
    "snippet": "3 days ago — Background: roflumilast cream 0.15% was studied …",
    "publication_info": {"summary": "GL Waldal, A Smith - Journal of Cutaneous Immunology, 2026 - frontierspartnerships.org"}})
assert g["title"] == "Efficacy of roflumilast cream 0.15% in children with atopic dermatitis" and g["title_truncated"], g["title"]
assert g["doi"] == "10.3389/jcia.2026.17307" and g["authors"] == ["Waldal GL", "Smith A"], (g["doi"], g["authors"])
assert g["journal"] == "Journal of Cutaneous Immunology" and g["pub_date"] == "2026-01-01" and g["age_days"] == 3
assert g["scholar_snippet"].startswith("Background") and g["scholar_via"] == "frontierspartnerships.org"

# --- de-duplication across the three sources -------------------------------------
def rec(origin, title, **kw):
    base = dict(origin=origin, pmid=None, doi=None, title=title, abstract=None, sections=[], authors=["Liu J"], journal="J Dermatolog Treat",
                journal_abbrev="", pub_date="2026-08-01", pmc=None, pub_types=[], grants=[], coi="", issue="", language="eng",
                record_status="", url="", mesh=[], matched_terms=["Ruxolitinib topical"])
    base.update(kw)
    return base

T = "Real-world clinical experience with ruxolitinib cream versus systemic therapies in moderate atopic dermatitis"
store = {}; idx = S.Index(store)
S.merge(store, idx, rec("Google Scholar", T[:70], title_truncated=True, scholar_id="gs1", scholar_link="https://example.org/a"))
assert list(store) == ["scholar:gs1"]
S.merge(store, idx, rec("OpenAlex", T, doi="10.1080/x.1", openalex_id="W1", abstract="Ruxolitinib cream in 619 adults."))
assert list(store) == ["doi:10.1080/x.1"] and store["doi:10.1080/x.1"]["scholar_link"] == "https://example.org/a"
S.merge(store, idx, rec("PubMed", T + ".", pmid="1", doi="10.1080/X.1", abstract="Ruxolitinib cream in 619 adults."))
m = store["1"]
assert list(store) == ["1"] and m["sources"] == ["PubMed", "OpenAlex", "Google Scholar"], (list(store), m["sources"])
assert m["aliases"] == ["doi:10.1080/x.1", "scholar:gs1"] and m["openalex_id"] == "W1" and m["scholar_link"] == "https://example.org/a"
assert S.decision_for("1", m, {"scholar:gs1": "removed"}) == "removed"
# near-identical titles that differ in a number, or carry different DOIs, are different articles
S.merge(store, idx, rec("Google Scholar", "Tapinarof cream in children with atopic dermatitis: 52-week results", scholar_id="gs2"))
S.merge(store, idx, rec("Google Scholar", "Tapinarof cream in children with atopic dermatitis: 8-week results", scholar_id="gs3"))
S.merge(store, idx, rec("OpenAlex", T, doi="10.9999/other", openalex_id="W2"))
assert sorted(store) == ["1", "doi:10.9999/other", "scholar:gs2", "scholar:gs3"], sorted(store)
# ...but a repository copy (Zenodo, Figshare…) with its own DOI is the same article, and the journal's copy is kept
S.merge(store, idx, rec("OpenAlex", T, doi="10.6084/m9.figshare.1", openalex_id="W3"))
assert sorted(store) == ["1", "doi:10.9999/other", "scholar:gs2", "scholar:gs3"] and store["1"]["doi"] == "10.1080/X.1"
Z = "Perspectives on topical PDE4 inhibitor drugs from development to submission"
zs = {}; zi = S.Index(zs)
S.merge(zs, zi, rec("OpenAlex", Z, doi="10.5281/zenodo.2", openalex_id="W5"))
S.merge(zs, zi, rec("OpenAlex", Z, doi="10.1016/j.real.2026.1", openalex_id="W6"))
assert list(zs) == ["doi:10.1016/j.real.2026.1"] and zs["doi:10.1016/j.real.2026.1"]["aliases"] == ["doi:10.5281/zenodo.2"]
# dedupe() folds such copies stored apart before they were recognised; the folded key stays as an alias
apart = {"doi:10.5281/zenodo.11": rec("OpenAlex", Z, doi="10.5281/zenodo.11", first_seen="2026-08-02"),
         "doi:10.5281/zenodo.12": rec("OpenAlex", Z, doi="10.5281/zenodo.12", first_seen="2026-08-01"),
         "77": rec("PubMed", Z + ".", pmid="77", doi="10.1016/j.real.2026.7", first_seen="2026-08-05"),
         "doi:10.9999/z": rec("OpenAlex", "Something else entirely about tapinarof", doi="10.9999/z")}
assert S.dedupe(apart) == 2 and sorted(apart) == ["77", "doi:10.9999/z"], sorted(apart)
assert apart["77"]["aliases"] == ["doi:10.5281/zenodo.11", "doi:10.5281/zenodo.12"] and apart["77"]["first_seen"] == "2026-08-01"
assert S.dedupe(apart) == 0
# OpenAlex records that are not articles (data files, front matter) are filtered
ds = S.parse_openalex_work({"id": "https://openalex.org/W9", "doi": "10.13130/rd_unimi/x/1", "display_name": "Tapinarof.pdb",
                            "type": "dataset", "publication_date": "2026-10-01"})
assert ds["oa_type"] == "dataset" and S.enrich(dict(ds, matched_terms=["Tapinarof"]), term_cat, rules)["rule"] == "Not an article (dataset or front matter)"
for title, kind, out in [("TRuE-AD3 52-week supplemental data", "dataset", True), ("Peer Review Report For: X", "peer-review", True),
                         ("Comparative Effectiveness of Topical Roflumilast and Tapinarof: A Network Meta-Analysis", "dataset", False),
                         ("Tapinarof cream in adolescents", "article", False)]:
    assert S.not_an_article(dict(origin="OpenAlex", oa_type=kind, title=title)) is out, title   # a real article OpenAlex calls a dataset stays

# a Scholar-only hit whose term is only in the full text is filtered; the merged record is not
tq = {t["term"]: S.plain_query(t["pubmed_query"]) for t in terms}
S.enrich(m, term_cat, rules, tq)
sc = S.enrich(rec("Google Scholar", "Psoriasis burden in primary care across Europe", sources=["Google Scholar"],
                  matched_terms=["Tapinarof"], scholar_snippet="… tapinarof was not available …"), term_cat, rules, tq)
assert m["rule"] is None and sc["rule"] == "Term only in full text (Google Scholar)" and "Google Scholar" in sc["summary"], sc["summary"]
row = S.to_page_row(m, None, key="1")
assert row["src"] == "PubMed, OpenAlex, Google Scholar" and row["links"][-1]["url"] == "https://example.org/a", row["links"]
assert row["first_seen"] == m["first_seen"][:19] and row["meta"].startswith("Liu J · J Dermatolog Treat 2026")
anon = S.to_page_row(S.enrich(rec("OpenAlex", "Tapinarof in practice", authors=[], journal="Figshare"), term_cat, rules), None, key="x")
assert anon["cite"] == "Figshare · 2026" and anon["meta"] == "Figshare 2026 · Article", (anon["cite"], anon["meta"])   # no "Unknown"

# --- feeds: a reviewer's decision wins over the exclusion rules ---------------------
assert S.page_status(None, None) == "review" and S.page_status("Veterinary", None) == "ignored"
assert S.page_status("Veterinary", "restored") == "review" and S.page_status("Veterinary", "accepted") == "accepted"
assert S.page_status(None, "ignored") == "ignored" and S.page_status(None, "removed") == "ignored"
why = "Must read: Own product: topical roflumilast"     # accepted automatically, unless a reviewer decided otherwise
assert S.page_status(None, None, why) == "accepted" and S.page_status(None, "restored", why) == "review"
assert S.page_status(None, "ignored", why) == "ignored" and S.page_status("Veterinary", None, why) == "accepted"
decided = {"42728891": "accepted", "42700001": "removed", "42600000": "restored"}
rows = [S.to_page_row(x, decided.get(x["pmid"])) for x in arts]
by_id = {r["id"]: r for r in rows}
assert by_id["42728891"]["status"] == "accepted" and by_id["42693807"]["status"] == "review"
assert by_id["42700001"]["status"] == "ignored" and by_id["42700001"]["decision"] == "ignored" and by_id["42700001"]["rule"] is None
assert by_id["42600000"]["status"] == "review" and by_id["42600000"]["rule"] == "Transplant / haematology context"
assert by_id["42600001"]["status"] == "ignored" and by_id["42600001"]["decision"] is None
assert by_id["42657782"]["status"] == "accepted" and by_id["42657782"]["auto"] == "Must read: Topical steroid harms"
assert by_id["42693807"]["prio"] == "Of interest" and by_id["42693807"]["score"] == 75 and by_id["42693807"]["auto"] is None
assert [h["id"] for h in by_id["42600002"]["l3i"]] == ["own_product", "own_product_mention"] and by_id["42600002"]["pen"][0]["points"] == -30

# --- articles sent in Articles of Interest emails: accepted, whatever the score or exclusion rules -------------
mail = lambda when, rec: dict(email_date=when, email_subject="INFORM: Articles of Interest", tier="main",
                              reviewer_note="Brett, please comment", record=json.dumps(rec))
known = dict(rec("PubMed", "Ruxolitinib for steroid-refractory chronic graft-versus-host disease", pmid="42600000"),
             sources=["Email"], first_seen="2026-02-02")
unknown = dict(rec("Email", "Clinical response to topical roflumilast in four patients with scarring alopecia", authors=["Modanlo N"],
                   journal="Dermatology Online Journal", pub_date="2026-01-01", url="https://escholarship.org/uc/item/x"),
               sources=["Email"], first_seen="2026-03-02T00:00:00")
mstore = {"42600000": dict(d)}; midx = S.Index(mstore)
assert S.add_emailed(mstore, midx, [mail("2026-10-16 15:02:00", unknown), mail("2026-03-02 09:00:00", known),
                                     mail("2026-06-01 09:00:00", known), dict(email_date="2026-05-01", record=None)]) == 1
new_key = S.record_key(unknown)
assert sorted(mstore) == ["42600000", new_key] and new_key.startswith("email:") and mstore[new_key]["sources"] == ["Email"]
assert mstore["42600000"].get("sources") == d.get("sources") and mstore["42600000"]["emailed"]["date"] == "2026-03-02"   # the first email
for x in mstore.values():
    S.enrich(x, term_cat, rules)
assert mstore["42600000"]["rule"] and mstore["42600000"]["auto_accept"] == "Sent in the Articles of Interest email of 2 Mar 2026"
mrow = S.to_page_row(mstore[new_key], None, key=new_key)
assert mrow["status"] == "accepted" and mrow["src"] == "Email" and mrow["via"] == "Dermatology Online Journal" and mrow["prio"] == "Must read"
assert mrow["emailed"] == dict(date="2026-10-16", subject="INFORM: Articles of Interest", tier="main", note="Brett, please comment")
assert S.to_page_row(mstore["42600000"], "restored")["status"] == "review"
assert S.add_emailed(mstore, midx, []) == 0 and not any("emailed" in x for x in mstore.values())   # an email row removed: unmarked

# --- reasons: keywords offered when accepting or ignoring, and the ones ticked --------------
assert by["42700001"]["keywords"] == ["Ruxolitinib", "Head-to-head"], by["42700001"]["keywords"]
assert by_id["42700001"]["keywords"] == ["Ruxolitinib", "Head-to-head"]
# title words count once; abstract-only words need two mentions (a single one is often a list of earlier treatments)
assert S.tag_keywords("Tapinarof cream in children", "Earlier dupilumab. Itch fell; itch NRS 2.") == ["Tapinarof", "Pediatric", "Itch"]
assert S.tag_keywords("Roflumilast cream versus vehicle", "EASI-75 in 52% vs 12%; compared with baseline") == ["Roflumilast"]
assert S.tag_keywords("Ruxolitinib cream versus systemic therapies", None) == ["Ruxolitinib", "Head-to-head"]
assert S.tag_keywords("Ruxolitinib for graft-versus-host disease", None) == ["Ruxolitinib"]
assert len(S.tag_keywords("Roflumilast tapinarof ruxolitinib delgocitinib orismilast crisaborole apremilast biologics phototherapy in children", None)) == 8
assert S.split_reasons(" Phase 3; Competitive intelligence ;;") == ["Phase 3", "Competitive intelligence"] and S.split_reasons(None) == []
r = S.to_page_row(by["42728891"], "accepted", reasons=["Roflumilast", "Safety signal"])
assert r["reasons"] == ["Roflumilast", "Safety signal"] and r["status"] == "accepted"
assert S.to_page_row(by["42600000"], "restored", reasons=["Stale"])["reasons"] == []    # back to review: no reasons
assert S.to_page_row(by["42700001"], "removed", reasons=["Off-target"])["reasons"] == ["Off-target"]
rows[0]["reasons"] = ["Roflumilast", "Safety signal"]

# --- settings saved from the page ---------------------------------------------------
with tempfile.TemporaryDirectory() as d:
    path = os.path.join(d, "settings.json")
    assert S.load_settings(path) == {}
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"terms": [{"term": " Tapinarof ", "product_category": "Competitor", "pubmed_query": "tapinarof[tiab]"},
                             {"term": "", "pubmed_query": "x[tiab]"}], "sources": ["pubmed"], "scholar_pages": 2,
                   "priority_keywords": [" child ", "", "Child", "scalp"]}, f)
    saved = S.load_settings(path)
assert saved["terms"] == [{"term": "Tapinarof", "product_category": "Competitor", "pubmed_query": "tapinarof[tiab]",
                           "openalex_query": "", "scholar_query": ""}] and saved["sources"] == ["pubmed"], saved
assert saved["priority_keywords"] == ["child", "scalp"], saved["priority_keywords"]

# --- Unity Catalog tables: rows carry exactly the columns of tables.sql, in order --------------------------
sql_text = open(os.path.join(ROOT, "tables.sql"), encoding="utf-8").read()
def columns(table):
    body = re.search(rf"literature_surveillance_{table} \((.*?)\n\)", sql_text, re.S).group(1)
    return re.findall(r"^  (\w+) ", body, re.M)
ar = S.article_row("42728891", a)
assert list(ar) == columns("articles"), (list(ar), columns("articles"))
assert json.loads(ar["record"]) == json.loads(json.dumps(a)) and ar["exclusion_rule"] is None and ar["disease"] == ["Atopic Dermatitis"]
assert str(ar["first_seen"]) == "2026-09-24 10:00:00" and S.article_row("x", dict(a, pub_date=""))["pub_date"] is None
assert ar["page_position"] is None and ar["page_row"] is None      # not on the page
on_page = S.article_row("42728891", a, 3)
assert on_page["page_position"] == 3 and json.loads(on_page["page_row"]) == S.to_page_row(a, None, key="42728891")
assert S.article_row("1", m)["aliases"] == ["doi:10.1080/x.1", "scholar:gs1"]
dr = S.decision_row({"pmid": "42700001", "decision": "removed", "by": "a@arcutis.com", "at": "2026-10-01T10:00:00+00:00", "reasons": "Off-target; Stale"})
assert list(dr) == columns("decisions") and dr["decision"] == "ignored" and dr["reasons"] == ["Off-target", "Stale"]
assert dr["decided_at"].isoformat() == "2026-10-01T10:00:00+00:00"
sr = S.settings_row(saved)
assert list(sr) == columns("settings") and sr["terms"] == [("Tapinarof", "Competitor", "tapinarof[tiab]", "", "")] and sr["scholar_pages"] == 2
assert all(re.findall(r"(\w+): STRING", s) == S.TERM_FIELDS for s in re.findall(r"ARRAY<STRUCT<(.*?)>>", sql_text))
run_settings = dict(saved, keys=dict(pubmed=True, openalex=False, scholar=True), l3_rules=S.DEFAULT_L3_RULES)
rr = S.run_row(run_settings, S.date(2026, 7, 30), S.date(2026, 10, 28), "medical_prod.apps.literature_surveillance_settings", 120, 4, 80)
assert list(rr) == columns("runs") and rr["api_keys"] == (True, False, True) and rr["terms"] == sr["terms"]
assert json.loads(rr["l3_rules"]) == S.DEFAULT_L3_RULES
assert S.page_header(["openalex", "pubmed"], S.date(2026, 7, 30), S.date(2026, 10, 28), S.datetime(2026, 10, 28, 13, 5)) == \
    "PubMed, OpenAlex surveillance · 30 Jul – 28 Oct 2026 · generated 28 Oct 2026 13:05"

# --- render: DATA and SETTINGS survive the trip into the page's script -------------------
rows[0]["summary"] = 'A "quoted" back\\slash, a\nnewline and </script>'
settings = dict(terms=S.clean_terms(terms), sources=["pubmed", "openalex"], scholar_pages=2, priority_keywords=["retrospective"],
                l3_rules=S.DEFAULT_L3_RULES, keys=dict(pubmed=True, openalex=False, scholar=False))
out = os.path.join(HERE, "page_offline.html")
S.render(rows, S.TEMPLATE, out, "PubMed surveillance · test window", settings=settings)
html = open(out, encoding="utf-8").read()
assert '"42728891"' in html and "PubMed surveillance · test window" in html
data = re.search(r"const DATA = (.*);\n", html).group(1)
assert json.loads(data.replace("<\\/", "</")) == rows and "</script>" not in data
page_settings = re.search(r"const SETTINGS = (.*);\n", html).group(1)
assert json.loads(page_settings) == settings and html.count("const SETTINGS = ") == 1
print("offline test OK ->", out)
for r in rows:
    print(f"  {r['prio']:11s} {r['access']:10s} {r['product']:11s} {r['status']:8s} {r['title'][:60]}")
