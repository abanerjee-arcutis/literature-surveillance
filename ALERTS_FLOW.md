# Copying alert emails into Databricks with Power Automate

Literature alert emails (Google Scholar, journal or vendor alerts) can reach the daily job without
Databricks ever reading a mailbox. A Power Automate flow watches for them and copies each one as a file
into a Unity Catalog volume, where the job can pick them up.

```
 alert email ──▶ Power Automate flow ──▶ Databricks Files API ──▶ volume literature_alerts_inbox ──▶ daily job
                (new email from one of                              one .json file per email
                 the alert senders)
```

- Databricks holds no mail credential.
- The flow's Databricks credential can only write into its own volume, not the article store or decisions.
- Only emails from the senders you list leave Outlook, and only five fields of each (sender, subject,
  received time, message id, body). Attachments and recipients stay behind.

Until the job is taught to read this volume, the files just accumulate there.

---

## Step 0 – Check you have what you need

| You need | Why | How to check |
|---|---|---|
| A Power Automate Premium licence (or a per-flow licence) | the flow uses the **HTTP** action, a premium connector | when you add the HTTP action it is labelled *Premium*; if the flow won't save or turn on, ask IT |
| HTTP allowed by your data-loss-prevention (DLP) policy | some tenants block the HTTP action | the flow checker says the flow *violates a DLP policy* |
| Power Automate able to reach the Databricks workspace | if the workspace only accepts connections from company IP addresses (IP access lists or private link), Power Automate's cloud is outside them | ask the Databricks admin |
| Workspace admin rights, or an admin to do Step 2 | creating a service principal | Databricks → your name → Settings: you see *Identity and access* |
| The sender address of each alert | the flow forwards only those | open an alert email and copy the From address, e.g. `scholaralerts-noreply@google.com` |

Message you can send your admins:

> I'd like a Power Automate flow to copy literature alert emails into a Databricks volume through the
> Files API. Could you confirm (1) I can use the premium HTTP action in Power Automate and DLP allows it
> to call https://adb-7405608290672797.17.azuredatabricks.net, (2) the workspace accepts connections
> from Power Platform, and (3) a Databricks-managed service principal `lit-surv-alerts-writer` can be
> created, with WRITE VOLUME on one new volume only?

---

## Step 1 – Create the inbox volume

In Databricks, **SQL Editor** → paste → **Run**:

```sql
CREATE VOLUME IF NOT EXISTS medical_prod.apps.literature_alerts_inbox
COMMENT 'Alert emails copied in by Power Automate; read by the literature surveillance job';
```

It is separate from `literature_surveillance` on purpose: the flow can write here and nowhere else.

---

## Step 2 – Create a service principal for the flow

A service principal is a non-human account. The flow signs in to Databricks as it.

1. Databricks → your name (top right) → **Settings** → **Identity and access** → **Service principals**
   → **Manage** → **Add service principal** → **Add new**.
2. Choose **Databricks managed**, name it `lit-surv-alerts-writer`, click **Add**.
3. Open it → **Secrets** tab → **Generate secret** → lifetime 730 days (the maximum). Under **Scopes**,
   choose **Other APIs**, search for `files` and select it; nothing else, and not *all APIs*. (*BI tools*
   is for Power BI and similar.) The secret can then only call the Files API. **Generate**, then copy the
   **Client ID** and the **Secret** now; the secret is shown only once. Put a reminder in your calendar
   to generate a new one before it expires.
4. In the **SQL Editor**, replace `<client-id>` with the Client ID and run:

```sql
GRANT USE CATALOG ON CATALOG medical_prod TO `<client-id>`;
GRANT USE SCHEMA ON SCHEMA medical_prod.apps TO `<client-id>`;
GRANT READ VOLUME, WRITE VOLUME ON VOLUME medical_prod.apps.literature_alerts_inbox TO `<client-id>`;
```

That is all it gets: no access to the job, the app or the article store.

---

## Step 3 – Build the flow

In a browser, go to make.powerautomate.com → **My flows** → **New flow** → **Automated cloud flow**.
Use the website, not the *Power Automate* desktop app: the app builds desktop flows, which run only
while your PC is on and would read mail over IMAP instead.

- Flow name: `Literature alerts to Databricks`
- Trigger: search for **When a new email arrives (V3)** (Office 365 Outlook) to watch your own inbox,
  or **When a new email arrives in a shared mailbox (V2)** to watch a shared mailbox.
- Click **Create**.

### 3a – The trigger

Click the trigger and set (some fields are under *Advanced parameters*):

| Field | Value |
|---|---|
| Original Mailbox Address | the shared mailbox (shared-mailbox trigger only) |
| Folder | Inbox |
| From | **for now**, your own email address (for the test in Step 4) |
| Subject Filter | **for now**, `litsurv test` |
| Include Attachments | No |
| Only with Attachments | No |

### 3b – Get a Databricks token

Click **+** under the trigger → **Add an action** → search `HTTP` → choose **HTTP**. Rename it (**…** →
**Rename**) to `Get token`, exactly, because the next action refers to it by that name.

| Field | Value |
|---|---|
| URI | `https://adb-7405608290672797.17.azuredatabricks.net/oidc/v1/token` |
| Method | `POST` |
| Headers | key `Content-Type`, value `application/x-www-form-urlencoded` |
| Body | `grant_type=client_credentials&scope=files` |
| Authentication (Advanced parameters) | **Basic**. Username: the Client ID. Password: the Secret |

Then open the action's **Settings** tab and turn on **Secure inputs** and **Secure outputs**, which keeps
the secret and the token out of the run history. Anyone who can edit the flow can still see the secret,
so keep the owner list short.

### 3c – Upload the email to the volume

Add another **HTTP** action below `Get token` and rename it `Upload to volume`.

- **Method:** `PUT`
- **URI:** click the field → **fx** (Insert expression) → paste the line below → **Add**:

  ```
  concat('https://adb-7405608290672797.17.azuredatabricks.net/api/2.0/fs/files/Volumes/medical_prod/apps/literature_alerts_inbox/', formatDateTime(utcNow(), 'yyyyMMdd-HHmmss'), '-', guid(), '.json')
  ```

- **Headers:** two rows.
  - Key `Authorization`. For the value, click → **fx** → paste → **Add**:

    ```
    concat('Bearer ', body('Get_token')?['access_token'])
    ```

  - Key `Content-Type`, value `application/octet-stream`.
- **Body:** click → **fx** → paste → **Add**:

  ```
  string(setProperty(setProperty(setProperty(setProperty(setProperty(json('{}'), 'message_id', triggerBody()?['internetMessageId']), 'from', triggerBody()?['from']), 'subject', triggerBody()?['subject']), 'received', triggerBody()?['receivedDateTime']), 'html', triggerBody()?['body']))
  ```

- **Authentication:** None (the Authorization header does it).
- **Settings** tab: turn on **Secure inputs**.

Click **Save**. The **Flow checker** (top right) should show no errors.

---

## Step 4 – Test it

1. From the address you put in **From**, send an email to the mailbox the trigger watches (the account
   shown under *Connected to*) with the subject `litsurv test` and a link or DOI in the body.
2. Wait a few minutes (the trigger checks the inbox every minute or so). Open the flow's page. The run
   history should show a run that *Succeeded*.
3. In Databricks: **Catalog** → `medical_prod` → `apps` → `literature_alerts_inbox`. You should see a
   file such as `20261006-143012-…json`. From the terminal:
   `databricks fs ls dbfs:/Volumes/medical_prod/apps/literature_alerts_inbox`
4. Point the flow at the real alerts: edit the trigger, set **From** to the alert senders separated by
   `;` (e.g. `scholaralerts-noreply@google.com`), clear **Subject Filter**, and **Save**.
5. Delete the test file: `databricks fs rm dbfs:/Volumes/medical_prod/apps/literature_alerts_inbox/<file name>`
6. Add a co-owner (flow page → **Edit** under *Owners*) so the flow doesn't stop if you're away or leave.

---

## Troubleshooting

Open the failed run, then click the red action to see its error.

| What you see | What to do |
|---|---|
| Flow won't save or turn on: *premium* or *licence* | you need the licence from Step 0 |
| Flow won't save: *violates DLP policy* | ask IT to allow the HTTP action for the workspace URL |
| `Get token`: 401 `invalid_client` | the Client ID or Secret is wrong or expired; generate a new secret (Step 2.3) and paste it into `Get token` |
| `Get token`: 400 `invalid_scope` | the Body asks for a scope the secret doesn't have; it must say `scope=files` |
| `Upload to volume`: 403 mentioning a *scope* | the secret needs one more scope; generate a new secret with `files` **and** `unity-catalog` (scopes can't be added to an existing secret), and change the Body of `Get token` to `scope=files unity-catalog` |
| `Get token` or `Upload to volume`: timeout, or 403 mentioning an IP address | the workspace doesn't accept connections from Power Platform; ask the Databricks admin |
| `Upload to volume`: 403 `PERMISSION_DENIED` | one of the three grants in Step 2.4 is missing, or was given to a different ID |
| `Upload to volume`: 404 `NOT_FOUND` | the volume path in the URI doesn't match Step 1 |
| `Upload to volume`: 401 | `Get token` was renamed after the expressions were added; check the Authorization expression still says `Get_token` |
| No runs at all after an alert arrives | the From address doesn't match the sender exactly, or the alert went to another folder (Clutter, Other, a rule's folder) |
| Runs stop weeks later: *connection* or *unauthorized* on the trigger | the Outlook connection needs signing in again: edit the flow → trigger → **Change connection** |

---

## If IT prefers an Entra ID service principal

Skip `Get token`. Ask IT to add an Entra app registration to the workspace as a service principal
(same grants as Step 2.4). In `Upload to volume`, remove the `Authorization` header and set
**Authentication** to **Active Directory OAuth**, with Tenant set to your tenant ID, Audience set to
`2ff814a6-3304-4ab8-85cb-cd0e6f879c1d` (the fixed ID for Azure Databricks), and Client ID and Secret
from the app registration.
