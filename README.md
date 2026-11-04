# Literature surveillance (PubMed, OpenAlex, Google Scholar) – working model

One script. Searches PubMed, OpenAlex and Google Scholar for a list of terms, merges the same article
found by several sources into one record, filters with exclusion rules, tags each article (disease,
product, study type, access level, sponsor, priority), writes a summary from the abstract, and renders
the Literature Update page.

To run it daily on Databricks with a shared review page, follow [DEPLOY.md](DEPLOY.md).

## Run

```
pip install requests
python surveillance.py --api-key YOUR_NCBI_KEY --serpapi-key YOUR_SERPAPI_KEY --email you@arcutis.com
```

Each run searches the last 90 days (`--days`). Open `literature_update.html`. Run it again tomorrow, next week, whenever: only new articles are
fetched; everything already seen is kept in `data/articles.json` and re-tagged with the current
terms and rules.

## The page

- **Header** – the sources the last run searched (Google Scholar only when it had a SerpApi key), its window, when
  it ran (in the viewer's time zone) and how many articles it found for the first time. A warning appears when the
  last run is over 30 hours old. The viewer's email is shown on the right: decisions are saved under it.
  *How it works* explains the page to a first-time reader.
- **Review feed** – new articles waiting for a decision, highest L3 score first, grouped by priority. The Low band
  is folded away at the end (*Show the … Low-priority articles*) unless it is all there is, or the Priority filter
  or a search asks for it. ✓ accepts an article, ✕ ignores it. Each row shows its matched term, disease, intent
  (safety in red), access and priority (with its L3 score; hover it for the reasons). **New** marks articles a run
  first found in the last 7 days.
- **Why accept / why ignore** – ✓ and ✕ first ask why, with no free text: tick at least one chip under
  *Keywords in this article* (its disease, intent, study type and sponsor, plus the drugs, patient groups,
  body sites and evidence words found in its title and abstract) or *Reason* (a fixed list each for
  accepting and ignoring, `REASONS` in `app/template.html`). Enter confirms, Esc cancels. The ticks are saved
  with the decision, shown under the title in the accepted feed, in the Reason column of the ignored feed
  and on the article page (*Edit* there changes them), and counted by the Reason filter.
- **Accepted feed** – articles worth passing on to the team: those a reviewer accepted, plus two kinds accepted
  automatically, each with its own line under the title: ✉ every article sent in an INFORM Articles of Interest
  email (`ci_mails/`), whatever its score or the exclusion rules, and ★ every Must read article no exclusion rule
  ignored. *Move back to review* undoes an automatic accept (saved as `restored`); a reviewer's decision always wins.
  The article page shows the email's date and what it said about the article.
- **Ignored feed** – articles a reviewer ignored, plus those the exclusion rules filtered out. *Restore*
  sends one back to review (moving back to review asks nothing and clears the reasons).
- **Who decided** – every decision is saved with the reviewer's email and time (the app reads them from the
  `X-Forwarded-Email` header Databricks Apps adds). The article page says *Accepted by … on …*, and the tooltip
  of a row's reasons line says the same.
- **Sort and Export** – above each feed. *Sort* lists by priority (grouped by band), newest published, or most
  recently found. *Export* works on the feed as filtered and searched: *Copy digest* puts an email-ready list on
  the clipboard (grouped by priority; each article's linked title, citation, summary and why it is on the list),
  and *Download CSV* saves a spreadsheet with every field, including the L3 reasons and who decided.
- **Links** – the address names what is on screen (`#review`, `#accepted`, `#ignored`, `#article/<id>`), so the
  browser's back button works and *Copy link* on an article page gives colleagues a link straight to it.
- **Settings** (top right) has four tabs. **Sources** switches PubMed, OpenAlex and Google Scholar on or
  off, sets Scholar pages per term, and shows whether the last run had each API key. **L1 keywords** lists
  the search terms with their query for each source (OpenAlex and Google Scholar queries are derived from
  the PubMed one unless you type your own). **L2 intent** shows, read-only, what decides each intent and how
  many articles on the page have it. **L3 priority keywords** lists, read-only, the reasons to read and the
  push-down rules the last run scored with: each reason's keyword lists and points, and how many articles on the
  page it matched. Below them, your own L3 keywords can be added: an article with one in its title or abstract
  gets a reason worth 60 points and is marked ↑. Saved settings apply from the next run.

Above the feed tabs, a filters card holds the search box and one row of chips each for Published (last 30,
60 or 90 days, by publication date), Added (last 7 or 30 days, by when a run first found the article), Priority,
Disease, Intent, Source and Term, plus Reason (the reasons ticked,
most used first) on a feed that has any. Each chip shows how many articles in the current feed it would leave, given the other
filters; clicking a selected chip again clears it, and *Clear filters* resets them all. The chevron folds
the card down to the search box (its header then names the filters that are on); the browser remembers it. The filters apply
to all three feeds. Every move can be undone from the message at the bottom of the page.

Keys:

| Source | Key | Without it |
|---|---|---|
| PubMed | NCBI key: ncbi.nlm.nih.gov → Account Settings → API Key Management (free) | works at 3 requests/s instead of 10 |
| OpenAlex | none needed; `--openalex-key` if OpenAlex asks for one | works |
| Google Scholar | SerpApi key from serpapi.com (paid; Google has no Scholar API) | Scholar is skipped |

Keys can also come from environment variables (`PUBMED_API_KEY`, `SERPAPI_KEY`, `OPENALEX_API_KEY`) or,
on Databricks, from a secret scope (`--secret-scope`).

Useful options:

| Option | What it does |
|---|---|
| `--days 60` | look-back window in days (default 90) |
| `--since 2026-06-01 --until 2026-07-06` | exact window instead of `--days` |
| `--sources pubmed,openalex` | which sources to search (default: as saved in Settings, else all three) |
| `--scholar-pages 2` | pages of 20 Scholar results per term (default: as saved in Settings, else 1); each page is one SerpApi search |
| `--terms path.csv` | search terms from this CSV, even when terms were saved in Settings |
| `--show-days 180` | how far back the page lists articles (default 90) |
| `--no-fetch` | skip searching; re-tag and re-render the local store (after editing terms or rules) |
| `--rebuild` | re-download every stored PMID from PubMed (after a parser fix) |
| `--data-dir path` | where `articles.json`, `decisions.csv` and `settings.json` live (default `data/`) |
| `--tables catalog.schema.prefix` | keep the store, decisions and settings in the tables `prefix_articles`, `prefix_decisions` and `prefix_settings` instead, log each run in `prefix_runs`, and write no page: the Databricks app builds it from the tables (`tables.sql`; needs Spark, so on Databricks only). Files found in `--data-dir` are imported once, then renamed `*.imported`; with no settings saved yet, `terms.csv` is saved as the first |
| `--out path.html` | where to write the page |

## Files

| File | Purpose |
|---|---|
| `terms.csv` | one row per search term: display name, product category, PubMed query (`[tiab]` = title/abstract). Optional columns `openalex_query` and `scholar_query` override the queries derived from the PubMed one. Used until terms are saved in Settings |
| `exclusions.csv` | regex rules; `applies_to` is `title` or `title_abstract` |
| `app/template.html`, `app/page.py` | the reviewed wireframe, and the code that replaces its data block (used by the script for the page file and by the app) |
| `data/articles.json` | the growing store, one record per article (created on first run) |
| `data/decisions.csv` | optional; `pmid,decision` with `accepted`, `ignored` or `restored` (back to review despite an exclusion rule) to persist reviewer choices (`pmid` holds the article id shown on the page; the older `removed` still means ignored). The app also writes `by`, `at` and `reasons` (the chips ticked, separated by `;`), the record of why each article was accepted or ignored; files without the `reasons` column still work |
| `data/settings.json` | optional; the search terms, sources, Scholar pages and L3 priority keywords saved from the page's Settings. When present it replaces `terms.csv` and the default `--sources` / `--scholar-pages`; delete it to go back to `terms.csv` |
| `data/ci_articles.json` | optional; the articles sent in Articles of Interest emails, as the rows of the `_ci_articles` table (`ci_mails/build_tables.py --dry-run` writes them to `ci_mails/work/tables_payload.json`). Each is put on the accepted feed; one the search has not found is added to the store with the source *Email* |
| `ci_mails/` | how the L3 reasons were learned from the INFORM Articles of Interest emails, and the script that loads the `_intents` and `_ci_articles` tables (its own README) |
| `tests/test_offline.py` | parses fixtures and checks tagging, summaries, de-duplication and rendering without network |
| `databricks.yml` | Databricks bundle: the daily job and the review app |
| `tables.sql` | on Databricks, the three files above become the tables `medical_prod.apps.literature_surveillance_articles` (one row per article; the whole record is in its `record` column, and the page's copy in `page_row`), `_decisions` (one row per decided article, with `reasons` as an array) and `_settings` (one row per Save; the newest applies), plus `_runs` (one row per run: what it searched and found) |
| `app/` | the Databricks app that builds the page from those tables on every visit and saves Accept/Ignore/Restore clicks and Settings to them |

## How the three sources are combined

- **Queries** – the PubMed query is translated for the other two: field tags and `*` dropped, multi-word terms quoted (`topical steroid withdrawal[tiab]` → `"topical steroid withdrawal"`).
- **PubMed** – searched by entry date, as before.
- **OpenAlex** – searched by publication date. OpenAlex searches full text too, so hits are kept only when the query matches the title or abstract (the equivalent of `[tiab]`). Hits that carry a PMID are fetched from PubMed, so they get PubMed's abstract, MeSH and PMC full text.
- **Google Scholar** – via SerpApi, newest first; hits are kept while the date Scholar added them falls in the window. Scholar returns only a title, link and snippet, so each new hit is looked up in OpenAlex (by DOI in the link, else by title); if that finds a PMID, the PubMed record is used. Scholar-only hits whose term is not in the title or abstract go to the *Ignored feed* with the rule "Term only in full text (Google Scholar)".
- **De-duplication** – two hits are the same article when they share a PMID, a DOI, or a title (ignoring case, punctuation and markup; titles must also have the same numbers and a publication year within one year, and Scholar's shortened titles match by prefix). Records with different PMIDs or DOIs are never merged, except that a repository DOI (Zenodo, Figshare, OSF, Dryad, Dataverse: `REPOSITORY_DOI` in `surveillance.py`) does not count, since such a copy of a journal article, often one per version, has its own DOI; the journal's copy is kept. Every run first folds together stored records that these rules say are the same article (`dedupe`), keeping the folded record's id as an alias so decisions follow it. OpenAlex records that are not articles (datasets, front matter, peer reviews) are ignored with the rule *Not an article*. The merged record keeps the metadata of the best source (PubMed, then OpenAlex, then Scholar), and its **Source** lists every feed that found it. If an article first seen in Scholar or OpenAlex later appears in PubMed, it keeps its first-seen date and any reviewer decision.

## How each field is derived

- **Matched term** – every term whose query returned the article (an article can match several, from any source).
- **Disease** – keyword match on title, abstract and MeSH (psoriasis, atopic/eczema, seborrheic, hidradenitis, vitiligo) else *Other*.
- **Product** – Roflumilast if any matched term is a roflumilast term, else Competitor, else Class.
- **Study type** – PubMed `PublicationType` first (Phase 3, RCT, Case Reports, Observational Study, Review…), OpenAlex work type for OpenAlex-only records, then keywords.
- **Intent** – what the article is about: *Safety*, *Efficacy*, *Real-world use*, *Mechanism*, *Disease state*, *Patient perspective*, *Review & guidance* or *Other*. Rules in `INTENT_RULES` (in `surveillance.py`) are tried in order against the study type and title; the first match wins. Safety words (adverse, toxicity, allergic, *drug*-induced, *after … therapy*…) count only when the title does not also claim efficacy or treating the event, so "Efficacy and safety of …" trials are *Efficacy*. Case reports with no other match are *Efficacy* when the title names a treatment ("for", "with"), else *Disease state*. The abstract is used only when the title and study type say nothing, since nearly every abstract mentions safety and efficacy. The page shows which rule decided it (article page → *Intent*).
- **Access** – *Full text* when the open-access full text was retrieved from PMC (via E-utilities `db=pmc`), *Abstract* when PubMed, OpenAlex or PMC has one, else *Title only*. Each article carries up to five links: PMC full text, publisher page (DOI), PubMed record, another open-access copy (OpenAlex), and the page Google Scholar pointed to.
- **Sponsor** – from the conflict-of-interest statement, grant agencies / OpenAlex funders, or a company name in the abstract; else *Not stated*.
- **Keywords** (offered in the why-accept / why-ignore dialog) – `KEYWORDS` in `surveillance.py`: drugs and drug classes, patient groups (pediatric, infants, skin of color…), body sites (face, scalp, intertriginous…) and evidence words (long-term, head-to-head, itch, quality of life…). A keyword counts when it is in the title, or mentioned at least twice in the abstract (a single mention is often a list of earlier treatments); at most eight per article.
- **Priority** – the L3 score, learned from the INFORM Articles of Interest emails (`ci_mails/`). `L3_INTENTS` in `surveillance.py` holds 23 reasons to read (own product 100 points; comparisons and guidelines 85; competitor safety and topical steroid harms 80; seborrheic dermatitis and TSW 75; … pregnancy, pipeline 50), each a set of keyword lists that must all match, in the title or in the title and abstract, with words that rule it out; `L3_PUSH_DOWN` holds 9 rules that take points off what was never sent (congress abstracts, Arcutis's own trial publications and funded work, oral JAK inhibitors, vitiligo and alopecia areata, lab studies, steroid phobia and social media, non-English, book chapters and preprints). Score = points of the best reason + 5 for each further reason (at most 15 more) − push-down points. **Must read** 85+, **Of interest** 60–84, **FYI** 40–59, **Low** under 40, or whenever an exclusion rule fired. Keywords match as phrases, ignoring case and punctuation, words as prefixes. With `--tables` the `_intents` table replaces the built-in list when it exists. Feeds list the highest score first.
- **Summary / key points** – articles with no abstract but PMC full text (common for case reports) are summarised from the paper body: opening sentences, closing sentences of the discussion/conclusion, and sentences with figures as key points. For structured abstracts: one sentence each from Background, Methods and Conclusions; key points are the Results sentences containing numbers. Unstructured abstracts: first two and last sentence. Abstract-only items say so. No language model is involved, so nothing is invented; the wording is the authors' own.
- **Why it matters** – templated from product, disease, study type and the caveats above (sponsor, single case, indication outside the core set).

## Record formats and what the page says about them

PubMed is a citation index, not a full-text library. What the script can summarise depends on the record type:

| Record type | What PubMed holds | Page shows | Notes |
|---|---|---|---|
| Research article with abstract | title + abstract (+ PMC full text if open access) | Summary and key points | The normal case |
| Open-access article without an abstract (many case reports) | title + PMC full text | Summary built from the paper body | e.g. JAAD Case Reports |
| Conference abstract in a journal supplement | citation only (`Issue: Suppl`) | Title only, with the reason | The text lives in the publisher's PDF (e.g. BJD/BAD abstracts) |
| Letter, comment, editorial | title only | Title only, with the reason | No abstract exists for these formats |
| Ahead-of-print record | title, abstract often added days–weeks later | Title only at first; **re-fetched automatically** on later runs for 120 days | Status `Publisher` / `In-Process` |
| PMC record under embargo / not in the OA subset | PMC id but no readable body | Abstract or title only, with the reason | Author manuscripts have a 12-month embargo |
| Non-English article | translated title in [brackets], abstract sometimes absent | Language noted | |
| Retraction notice / erratum | notice record | Auto-removed (rule "Erratum / retraction notice") | A retracted article itself is labelled RETRACTED |
| Preprint (NIH preprint pilot) | abstract | Labelled Preprint | Not peer reviewed |
| NCBI Bookshelf chapter (StatPearls etc.) | chapter summary | Labelled Book chapter | Different XML; previously dropped silently |
| PMID listed by search but record not yet loaded | nothing | Reported in the terminal, fetched on a later run | Explains "N new" being higher than the store growth; if OpenAlex has it, its copy is shown meanwhile |
| Article not in PubMed but in OpenAlex | — | OpenAlex abstract if it has one (many publishers withhold abstracts from OpenAlex) | Non-MEDLINE journals, very recent articles |
| Article found only by Google Scholar | — | Title only, with Scholar's snippet | Grey literature, publisher pages before indexing |
| Article known only from an Articles of Interest email | — | Title only, with what the email said about it | UpToDate, trade journals, news; source *Email* |

## Limits of this version

- Google Scholar needs a paid SerpApi plan: one search per term per run (14 terms ≈ 420 searches a month on a daily schedule). Sort-by-date in Scholar covers about the last year.
- OpenAlex is searched by publication date, so an article indexed long after its publication date can be missed there (PubMed, searched by entry date, still catches it if it is in PubMed).
- Summaries are extractive. Swapping in a model summary is one function (`summarise`) if wanted later.
- Opened as a file, the page's Accept/Ignore/Restore buttons and the reasons ticked last for the browser session; to make a decision stick, add a line to `data/decisions.csv`. Settings' Save downloads `settings.json` instead; put it in `data/`. Served by the Databricks app, both are saved to the tables automatically.
- Settings change what future runs search for. Articles already found stay in the store and on the page; renaming a term leaves older articles tagged with the old name.
