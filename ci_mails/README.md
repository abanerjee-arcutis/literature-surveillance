# CI mails: the L3 reasons, learned from the Articles of Interest emails

Learns why Medical Affairs flags an article from the INFORM "Articles of Interest" emails. The result is the L3
priority of the daily job: `L3_INTENTS` (reasons to read: keyword groups with points) and `L3_PUSH_DOWN` (rules that
take points off) in `surveillance.py`. The job scores every article with them, puts Must read articles on the
accepted feed, and lists them in the page's Settings → L3 priority keywords.

| Step | Script | Output (in `work/`, gitignored) |
|---|---|---|
| 1. Read the emails (PDFs; copies in `/Volumes/medical_prod/apps/ci_mails`) | `parse_emails.py <pdf dir>` | `email_articles.json`: one record per cited article, with tier, links and the reviewer's note |
| 2. Find each in PubMed and OpenAlex | `resolve.py` | `email_resolved.json` |
| 3. Fetch what the L1 terms find over the same months | `l1_pool.py` | `l1_pool.json`: the articles the reviewer chose from |
| 4. Score both with the L3 reasons; compare with the old priority rule | `backtest.py [--list-misses] [--list-false] [--gaps]` | printed |
| 5. Load the tables and create the view | `build_tables.py` (`--dry-run`: rows to `tables_payload.json` only) | `literature_surveillance_intents`, `_ci_articles`, `_ranked` (`intents.sql`) |

`build_tables.py` replaces the rows of both tables. `_intents` gets the reasons and push-down rules from
`surveillance.py` with their backtest evidence; when it exists, the daily job scores with its active rows instead
of the built-in list, so a reason can be re-weighted or switched off in SQL. `_ci_articles` gets every emailed
article with the record the job stores; the job puts each on the accepted feed, adding to its store the ones its
search has not found (source *Email*). `_ranked` lists the job's articles by L3 score with the feed each is on.

To change the reasons for good, edit `L3_INTENTS` / `L3_PUSH_DOWN`, re-run `backtest.py`, then `build_tables.py`.
After new emails, run steps 1, 2 and 5 (`resolve.py` resumes from its cache in `work/`).

`resolve.py` and `l1_pool.py` read the OpenAlex key from `OPENALEX_API_KEY` (secret scope `lit-surv`, key
`openalex-api-key`); Databricks access uses the CLI's default profile, or `DATABRICKS_CONFIG_PROFILE`. Needs `pypdf`
and `databricks-sdk` besides `requests`.

Scoring: a reason matches when each of its keyword groups has a keyword in its field (title, or title + abstract)
and none of its `none_of` words is in the title; keywords match like the page's L3 priority keywords (case and
punctuation ignored, words as prefixes). Score = points of the best reason + 5 per further reason (at most +15) −
the points of the push-down rules that fire. Must read 85+, Of interest 60–84, FYI 40–59, Low below 40.
