# Deploying to Databricks, step by step

This guide assumes you have never deployed anything to Databricks. Expect about an hour the first time,
most of it waiting for access or keys.

## What you will end up with

```
  every day 06:00                 Unity Catalog: medical_prod.apps                     browser
 ┌────────────────┐        ┌────────────────────────────────────────────┐          ┌──────────────┐
 │ Job            │ ─────▶ │ literature_surveillance_articles    table  │ ───────▶ │ App          │ ◀── you and
 │ surveillance.py│ ─────▶ │ literature_surveillance_runs        table  │ ───────▶ │ builds the   │     reviewers
 │                │ ◀───── │ literature_surveillance_decisions   table  │ ◀──────▶ │ page on each │
 │                │ ◀───── │ literature_surveillance_settings    table  │ ◀──────▶ │ visit        │
 └────────────────┘        └────────────────────────────────────────────┘          └──────────────┘
```

- A **job** runs `surveillance.py` every morning. It searches PubMed, OpenAlex and Google Scholar,
  merges duplicates, and writes the articles to the **articles table** and what it did to the **runs table**.
- An **app** (a private web page inside Databricks) builds the Literature Update page from the tables
  each time someone opens it. Accept / Ignore / Restore clicks, with the reasons ticked for each accept
  or ignore, are saved to the **decisions table**, and the Settings dialog (search terms and sources) to
  the **settings table**, so the next run uses them. The app runs its SQL on a **SQL warehouse**.
- The tables are the only place the data lives; no files are written. Being tables, they can also be
  queried in the SQL editor, dashboards or Genie, e.g. `SELECT * FROM medical_prod.apps.literature_surveillance_decisions`.
- One file, `databricks.yml`, describes both. A single command (`databricks bundle deploy`) uploads the
  code and creates or updates the job and the app.

---

## Step 0 – Check you have what you need

| You need | Why | How to get it |
|---|---|---|
| Your Databricks workspace URL | to log in from the command line | open Databricks in the browser and copy the address up to `.com` or `.net`, e.g. `https://adb-1234567890.12.azuredatabricks.net` |
| Permission to create **jobs**, **apps**, and **tables** in a schema, with **serverless compute**, and **Can use** on a **SQL warehouse** | the job and app run on it | ask your Databricks admin (text below) |
| Outbound internet from serverless compute to `eutils.ncbi.nlm.nih.gov`, `api.openalex.org`, `serpapi.com` | the job calls these APIs | same admin |
| NCBI API key (free) | faster PubMed access | ncbi.nlm.nih.gov → sign in → Account Settings → API Key Management |
| SerpApi key (paid) | the only practical way to query Google Scholar | serpapi.com → sign up → choose a plan. A daily run uses about 14 searches (one per term), about 420 a month, so check that the plan covers it. **Check with IT/legal before buying**: SerpApi is a third-party service that scrapes Google. Without a key, everything works except Scholar. |
| Python 3 on your laptop | to test before deploying | you already ran the script locally, so you have it |

Message you can send your admin:

> I'd like to deploy a small Python job and a Databricks App for literature surveillance. Could you
> confirm I can (1) create jobs on serverless compute, (2) create Databricks Apps, (3) create four tables
>  in medical_prod.apps, (4) create a secret scope, (5) use a SQL warehouse (which one
> should the app use?), and (6) that serverless compute can reach eutils.ncbi.nlm.nih.gov,
> api.openalex.org and serpapi.com over HTTPS?

---

## Step 1 – Test on your laptop first

Open PowerShell (or the VS Code terminal) in the project folder:

```powershell
cd "C:\Users\AyanBanerjee\Documents\Apps\literature-surveillance"
copy data\articles.json data\articles.backup.json      # safety copy of your current store
pip install requests
python tests\test_offline.py
```

You should see `offline test OK`. Then do a real run over one week:

```powershell
python surveillance.py --days 7 --api-key YOUR_NCBI_KEY --serpapi-key YOUR_SERPAPI_KEY --email you@arcutis.com
```

Each term prints a count per source, e.g. `Tapinarof   PubMed    6 · OpenAlex    8`, followed by a
Google Scholar line per term. At the end, `sources: …  found by more than one (shown once)` tells you
how many duplicates were merged. Open `literature_update.html` and check the **Source** chips
(PubMed / OpenAlex / Google Scholar).

If this works, the code is fine and anything that fails later is about Databricks setup.

---

## Step 2 – Install the Databricks command-line tool (CLI) and log in

```powershell
winget install Databricks.DatabricksCLI
```

Close and reopen the terminal, then check it is installed:

```powershell
databricks --version
```

(If `winget` is blocked on your laptop, download the Windows zip from
github.com/databricks/cli/releases, unzip `databricks.exe` into a folder, and add that folder to your PATH,
or ask IT to install it.)

Log in (replace the URL with yours from Step 0):

```powershell
databricks auth login --host https://adb-7405608290672797.17.azuredatabricks.net
```

A browser window opens. Sign in, then go back to the terminal and press Enter to accept the profile
name. Check it worked:

```powershell
databricks current-user me
```

You should see your user name and email.

---

## Step 3 – Create the storage: four tables

This guide uses the Medical Affairs schema `medical_prod.apps`. If yours is different, replace it
everywhere, including in `tables.sql`.

In Databricks, click **SQL Editor** in the left sidebar.

Open `tables.sql` from the project folder, paste all of it into the SQL editor, and click **Run**.
It creates `literature_surveillance_articles`, `_runs`, `_decisions` and `_settings`.
Running it again changes nothing.

Optional, for the L3 priority learned from the Articles of Interest emails: `ci_mails/build_tables.py` creates
and fills `literature_surveillance_intents` (the L3 reasons) and `_ci_articles` (the articles the emails sent),
and the view `_ranked` (`ci_mails/README.md`). Without them the job scores with the reasons built into
`surveillance.py` and puts no emailed articles on the page.

---

## Step 4 – Store your API keys as secrets

Secrets keep keys out of the code and out of the job settings. In the terminal:

```powershell
databricks secrets create-scope lit-surv
databricks secrets put-secret lit-surv pubmed-api-key --string-value "PASTE_YOUR_NCBI_KEY"
databricks secrets put-secret lit-surv serpapi-key --string-value "PASTE_YOUR_SERPAPI_KEY"
```

Check (this lists names only, never values):

```powershell
databricks secrets list-secrets lit-surv
```

If `create-scope` says you don't have permission, ask your admin to create the scope `lit-surv`
and give you WRITE on it.

---

## Step 5 – Nothing to do

Earlier versions kept data in files; the tables are now the only place data lives, so there is nothing to
copy. The first job run saves the terms in `terms.csv` as the first settings; from then on the settings
table is where terms are kept.

---

## Step 6 – Fill in your settings (three places)

1. **`databricks.yml`**, at the bottom: the workspace URL (already set to `https://adb-7405608290672797.17.azuredatabricks.net`).
2. **`databricks.yml`**, at the top under `variables`: set `catalog` and `schema` (if not `medical_prod`
   and `apps`), `notify_email` (failure emails go there), and under `warehouse_id` the name of a SQL
   warehouse you can use (**SQL Warehouses** in the sidebar; a serverless one starts fastest).
3. **`app/app.yaml`**: set `LITSURV_TABLES` to the tables' common start, e.g.
   `medical_prod.apps.literature_surveillance`.

The schedule is in `databricks.yml` too: `"0 0 6 * * ?"` with `America/Los_Angeles` means every day at 06:00
Pacific time. To run at 07:30 instead, use `"0 30 7 * * ?"`.

---

## Step 7 – Deploy

From the project folder:

```powershell
databricks bundle validate
databricks bundle deploy
```

`validate` checks the file for mistakes; fix anything it reports before deploying. `deploy` uploads the
code to your workspace folder (`/Workspace/Users/<you>/.bundle/lit-surv/prod/files`) and creates:

- the job **Literature surveillance (daily)** (under **Jobs & Pipelines** in the sidebar)
- the app **literature-surveillance** (under **Compute → Apps**)

---

## Step 8 – Run the job once now

Don't wait until tomorrow morning:

```powershell
databricks bundle run lit_surv_daily
```

It prints a link to the run and waits until it finishes (a few minutes). In the browser: **Jobs &
Pipelines** → **Literature surveillance (daily)** → the latest run → **Output**. You should see the same
per-term counts as on your laptop.

Then check the tables in the SQL editor: `SELECT * FROM medical_prod.apps.literature_surveillance_runs`
shows one row for this run, with the number of articles and how many are on the page.

---

## Step 9 – Start the app

```powershell
databricks bundle run lit_surv_app
```

This deploys the app code and starts it. It takes a minute or two and prints the app URL. You can also
find the URL under **Compute → Apps → literature-surveillance**. Open it. The first time, Databricks asks you to authorize
the app; accept.

You should see the Literature Update page. Accept an article, reload the page: it stays in the
Accepted feed, and `SELECT * FROM medical_prod.apps.literature_surveillance_decisions` shows it.

---

## Step 10 – Share it with your team

**Compute → Apps → literature-surveillance → Permissions** → add people or a group → **Can use**.

Viewers don't need access to the tables or the SQL warehouse. The app uses them with its own identity (a
"service principal" that Databricks created for the app), which `databricks.yml` gives read access to the
articles and runs tables and read/write access to the decisions and settings tables.

---

## Day to day

| You want to… | Do this |
|---|---|
| See if today's run worked | Jobs & Pipelines → the job → Runs. Failed runs also email `notify_email`. |
| Change search terms, sources or priority keywords | in the app: **Settings** (top right) → **Sources**, **L1 keywords** or **L3 priority keywords** → Save. The next run uses them; to apply now, `databricks bundle run lit_surv_daily`. Each Save adds a row to the settings table, so it is also the history of changes. To start again from `terms.csv`, empty that table (`DELETE FROM medical_prod.apps.literature_surveillance_settings`); the next run saves `terms.csv` as the first settings again. |
| Change an L3 reason or push-down rule (its points, keywords, or switch it off) | in the SQL editor, e.g. `UPDATE medical_prod.apps.literature_surveillance_intents SET weight = 80 WHERE intent_id = 'seb_derm'` (or `SET active = false`). The next run scores with it and Settings → L3 lists it. To start again from the list in `surveillance.py` (`L3_INTENTS`, `L3_PUSH_DOWN`), run `ci_mails/build_tables.py` |
| Add the articles of new Articles of Interest emails | save the emails as PDF to `/Volumes/medical_prod/apps/ci_mails`, then run the `ci_mails` steps (`ci_mails/README.md`); the next run puts them on the accepted feed |
| See what each run searched and found | `SELECT * FROM medical_prod.apps.literature_surveillance_runs ORDER BY run_at DESC` |
| Change exclusion rules | edit `exclusions.csv` → `databricks bundle deploy`. The next run uses them; to apply now, `databricks bundle run lit_surv_daily`. |
| Change the code or the page template (`app/template.html`) | edit, run `python tests\test_offline.py`, then `databricks bundle deploy`, plus `databricks bundle run lit_surv_app` for the template |
| Change the app | edit `app/` → `databricks bundle deploy` → `databricks bundle run lit_surv_app` |
| Pause the daily run | job page → **Schedules & Triggers** → Pause (or set `pause_status: PAUSED` and deploy) |
| Stop paying for the app while unused | `databricks apps stop literature-surveillance`; start again with `databricks bundle run lit_surv_app` |

### Upgrading a deployment made before the L3 score (November 2026)

Priority is now the L3 score (Must read, Of interest, FYI, Low) instead of High / Medium / Check / Low, and Must
read articles and those sent in Articles of Interest emails go to the accepted feed by themselves. Once:

1. In the SQL editor, add the runs table's new column (the last statement in `tables.sql`, commented out):
   `ALTER TABLE medical_prod.apps.literature_surveillance_runs ADD COLUMNS (l3_rules STRING)`. Without it the
   job still runs, but Settings → L3 cannot list the reasons.
2. `cd ci_mails` and `python build_tables.py`: creates and fills `_intents` and `_ci_articles`.
3. `databricks bundle deploy`, then `databricks bundle run lit_surv_daily` (rescores every article and adds the
   emailed ones) and `databricks bundle run lit_surv_app`.

Decisions already saved keep working: a reviewer's decision always wins over the automatic ones.

**Costs:** the job uses serverless compute for a few minutes a day. An app bills for every hour it is
running, whether or not anyone looks at it. Ask your admin what that costs in your workspace.

---

## Troubleshooting

| What you see | What to do |
|---|---|
| `bundle validate` complains about `uc_securable` or `environment_version` | update the CLI (`winget upgrade Databricks.DatabricksCLI`) and try again. If it still fails: for `uc_securable`, delete the app's `resources:` block in `databricks.yml` and grant access by hand (row below). |
| Job output: `ConnectionError`, `Max retries exceeded`, or a timeout to eutils / openalex / serpapi | serverless compute can't reach the internet; ask your admin to allow those three domains |
| Job output: `Google Scholar skipped: no SerpApi key` | the `serpapi-key` secret is missing or misspelled (Step 4) |
| Job output: `PUBMED_API_KEY does not look like an NCBI key` | re-enter the `pubmed-api-key` secret (Step 4) |
| Job output: `Google Scholar ERROR … run out of searches` | your SerpApi plan is used up; upgrade it, or switch Google Scholar off in the app (**Settings → Sources**) |
| Job fails: `Table medical_prod.apps.literature_surveillance_… does not exist or you can't read it` | run `tables.sql` (Step 3). If it exists, you need SELECT and MODIFY on the four tables |
| Deploy says `CATALOG_DOES_NOT_EXIST` | the catalog or schema in `databricks.yml` doesn't exist. `databricks catalogs list` shows the catalogs you have; use the same names in `app/app.yaml` |
| App page says **The database is starting up** | the SQL warehouse was stopped and took over two minutes to start; reload. If it happens often, raise the warehouse's auto-stop time (SQL Warehouses → the warehouse → Edit) |
| App page says **The Literature Update could not be loaded** | a query failed; its message is under *Technical detail*. Often a permission (rows below) or a table renamed in `app/app.yaml` |
| App page says **No articles yet** | the runs table is empty: the job hasn't run since the tables were created; run Step 8, or check `LITSURV_TABLES` in `app/app.yaml` |
| App shows an error mentioning permission, `403`, `TABLE_OR_VIEW_NOT_FOUND` or `INSUFFICIENT_PERMISSIONS` | give the app's service principal access. Run `databricks apps get literature-surveillance` and copy `service_principal_client_id`, then in the SQL editor: <br>`GRANT USE CATALOG ON CATALOG medical_prod TO \`<that id>\`;`<br>`GRANT USE SCHEMA ON SCHEMA medical_prod.apps TO \`<that id>\`;`<br>`GRANT SELECT ON TABLE medical_prod.apps.literature_surveillance_articles TO \`<that id>\`;`<br>`GRANT SELECT ON TABLE medical_prod.apps.literature_surveillance_runs TO \`<that id>\`;`<br>`GRANT SELECT, MODIFY ON TABLE medical_prod.apps.literature_surveillance_decisions TO \`<that id>\`;`<br>`GRANT SELECT, MODIFY ON TABLE medical_prod.apps.literature_surveillance_settings TO \`<that id>\`;`<br>and on the SQL warehouse's **Permissions**, give it **Can use** |
| `bundle validate` or `deploy` says the warehouse can't be found | `warehouse_id` in `databricks.yml` must name a SQL warehouse exactly as **SQL Warehouses** lists it, and you need **Can use** on it |
| `validate` or the job says serverless isn't available | ask your admin to enable serverless jobs, or for a cluster policy to use. With a policy, replace `environment_key: default` in the task with `job_cluster_key: small` and add a `job_clusters` section (your admin can give you the exact cluster settings) |
| A colleague can't open the app | add them under the app's **Permissions** with **Can use** (Step 10) |
