# QuinnStreet Outbound Analytics Data Collection and Dashboard Import Run Book

## 1. Purpose

This run book describes the repeatable process for collecting QuinnStreet outbound campaign conversation data from Genesys Cloud, processing the raw conversation data, importing it into the SQLite database, and refreshing the Node-RED Dashboard 2.0 application.

The supported project pipeline is:

```text
Genesys Cloud Analytics Job
        |
        v
conversations.jsonl
        |
        v
genesys_delivery_analysis_speechfix_v4.py
        |
        v
conversation_timings.csv
        |
        v
import_campaigns.py
        |
        v
data/outbound_dashboard.db
        |
        v
Node-RED Dashboard
```

The dashboard reads data from SQLite on demand. Campaign data is not loaded directly into Node-RED global context.

---

## 2. Prerequisites

Before collecting data, confirm the workstation has:

- Node-RED with Dashboard 2.0 (`@flowfuse/node-red-dashboard`)
- Node.js and npm
- Python 3
- `curl`
- `jq`
- Access to the `OutboundCampaignDashboard` project
- A Genesys Cloud OAuth access token with the required Analytics permissions
- The Genesys Cloud region for the organization
- The Genesys Cloud outbound campaign ID
- The reporting interval to collect

From the project root, install the project dependencies if this has not already been done:

```bash
npm install
```

The project repository is:

```text
git@github.com:James-Rhodes-G/OutboundCampaignDashboard.git
```

The local dashboard URL is:

```text
http://127.0.0.1:1880/dashboard/overview
```

---

## 3. Project Data Location

Campaign data is stored under the data directory configured by `dataDir` in:

```text
scripts/campaigns.json
```

The default is:

```text
data/campaigns
```

A campaign directory has the following structure:

```text
data/campaigns/
├── outbound_campaigns.csv
└── {CAMPAIGN_ID}/
    ├── conversations.jsonl
    └── conversation_timings.csv
```

The SQLite database used by the dashboard is:

```text
data/outbound_dashboard.db
```

If `scripts/campaigns.json` does not yet exist, create it from the supplied example:

```bash
cp scripts/campaigns.example.json scripts/campaigns.json
```

Then configure `dataDir` as required.

---

## 4. Identify the Campaign and Reporting Interval

Obtain the Genesys Cloud GUID for the outbound campaign being analyzed. Use the campaign ID rather than the campaign display name in the Analytics request.

The Analytics interval is expressed in ISO-8601 UTC format.

Example:

```text
2026-07-01T04:00:00.000Z/2026-08-01T04:00:00.000Z
```

This example represents July 1, 2026 00:00 EDT through August 1, 2026 00:00 EDT.

Use the UTC offset appropriate for the reporting period. Do not assume `04:00Z` for periods when Eastern Standard Time is in effect.

---

## 5. Create the Genesys Analytics Conversation Details Job

Submit the asynchronous conversation details job using:

```http
POST /api/v2/analytics/conversations/details/jobs
```

### Sample request body

Replace `<campaignId>` with the Genesys Cloud outbound campaign GUID and adjust the interval for the desired reporting period.

```json
{
  "segmentFilters": [
    {
      "predicates": [
        {
          "dimension": "outboundCampaignId",
          "propertyType": "string",
          "value": "<campaignId>"
        }
      ],
      "type": "and"
    }
  ],
  "interval": "2026-07-01T04:00:00.000Z/2026-08-01T04:00:00.000Z"
}
```

The response supplies the Analytics job ID. Record the `jobId`; it is used by the project collection script in the next step.

---

## 6. Collect the Genesys Conversation Results

The project includes the collection script:

```text
scripts/collect_genesys_conversations_access_token.sh
```

This is the standard project method for retrieving the conversation data for an Analytics job and writing it to JSONL.

### 6.1 Set the Genesys environment variables

From the project root:

```bash
export accessToken="your-oauth-token"
export GC_REGION="usw2.pure.cloud"
export OUTPUT_MODE=ndjson
```

Set `GC_REGION` to the region used by the QuinnStreet Genesys Cloud organization if it differs from the example.

Do not store the OAuth token in the repository. Pass it through the environment only.

### 6.2 Run the collection script

```bash
scripts/collect_genesys_conversations_access_token.sh \
  "<job-id-uuid>" \
  "data/campaigns/YOUR_CAMPAIGN_ID/conversations.jsonl"
```

Example:

```bash
scripts/collect_genesys_conversations_access_token.sh \
  "12345678-1234-1234-1234-123456789abc" \
  "data/campaigns/98765432-1234-1234-1234-123456789abc/conversations.jsonl"
```

If `dataDir` in `scripts/campaigns.json` is not `data/campaigns`, use the configured data directory in the output path.

### 6.3 Verify the output

Confirm that the following file exists and contains data:

```text
{dataDir}/{CAMPAIGN_ID}/conversations.jsonl
```

Retain this file. It is the raw conversation-level source data used by the timing analysis pipeline and provides traceability back to Genesys Cloud.

---

## 7. Ensure the Campaign Is Registered

The project uses:

```text
{dataDir}/outbound_campaigns.csv
```

as the campaign source and:

```text
scripts/campaigns.json
```

as the local campaign registry.

If the campaign is new, add its name and Genesys campaign GUID to `outbound_campaigns.csv`, then run:

```bash
npm run sync:campaigns
```

This refreshes `scripts/campaigns.json` from the campaign CSV.

For an existing campaign that is already configured, this step does not need to be repeated unless the campaign configuration has changed.

---

## 8. Build the Campaign Timing Data

After `conversations.jsonl` has been collected, generate the normalized timing CSV used by the database import.

From the project root, run:

```bash
python3 scripts/build_all_campaign_timings.py --campaign YOUR_CAMPAIGN_ID
```

Example:

```bash
python3 scripts/build_all_campaign_timings.py \
  --campaign 98765432-1234-1234-1234-123456789abc
```

`build_all_campaign_timings.py` processes the campaign's `conversations.jsonl` using:

```text
scripts/genesys_delivery_analysis_speechfix_v4.py
```

and creates:

```text
{dataDir}/{CAMPAIGN_ID}/conversation_timings.csv
```

The campaign directory should now contain:

```text
{CAMPAIGN_ID}/
├── conversations.jsonl
└── conversation_timings.csv
```

Do not proceed to the database import until `conversation_timings.csv` has been successfully created.

---

## 9. Import the Campaign into SQLite

The Node-RED dashboard queries a SQLite database rather than importing the CSV directly into dashboard flows.

Import the processed campaign data with:

```bash
python3 scripts/import_campaigns.py --campaign YOUR_CAMPAIGN_ID
```

Example:

```bash
python3 scripts/import_campaigns.py \
  --campaign 98765432-1234-1234-1234-123456789abc
```

The import script loads the campaign's `conversation_timings.csv` into:

```text
data/outbound_dashboard.db
```

The database schema is created automatically by `import_campaigns.py` on the first import.

Re-importing an existing campaign replaces the rows for that campaign in SQLite.

### Verify the import

List configured and imported campaigns with:

```bash
python3 scripts/import_campaigns.py --list
```

Confirm that the campaign appears in the imported campaign list before using the dashboard.

---

## 10. Refresh and Verify the Node-RED Dashboard

With Node-RED running, open:

```text
http://127.0.0.1:1880/dashboard/overview
```

After an import:

1. Open the Node-RED editor.
2. Open the **Dashboard** tab.
3. Click the **Refresh Dashboard** inject node.
4. Alternatively, redeploy the flow and hard-refresh the browser (`Cmd+Shift+R` on macOS).
5. Open **Overview**.
6. Select the imported campaign from the **Campaign** dropdown.
7. Confirm that the header shows the expected campaign and record count.
8. Review the Overview KPIs and charts for obvious data-quality issues.

Campaign options are loaded from the SQLite `campaigns` table.

---

## 11. Updating an Existing Campaign

To refresh an existing campaign for a new or expanded reporting period:

1. Create the new Genesys Analytics job for the desired interval.
2. Replace the campaign's `conversations.jsonl` with the newly collected export.
3. Rebuild the timing CSV:

```bash
python3 scripts/build_all_campaign_timings.py --campaign YOUR_CAMPAIGN_ID
```

4. Re-import the campaign:

```bash
python3 scripts/import_campaigns.py --campaign YOUR_CAMPAIGN_ID
```

5. Refresh the Node-RED dashboard.

Re-importing replaces all SQLite rows for that campaign, so the `conversations.jsonl` used for the rebuild must contain the complete dataset that should be represented for that campaign/reporting period.

---

## 12. Processing Multiple Campaigns

The project also supports bulk collection and processing.

### Collect all campaigns

Set the Genesys credentials and region:

```bash
export accessToken="your-oauth-token"
export GC_REGION="usw2.pure.cloud"
```

Then run:

```bash
scripts/collect_all_outbound_campaigns.sh
```

This uses `scripts/campaigns.json` to locate `dataDir` and uses `outbound_campaigns.csv` for the campaign set.

To limit collection to one campaign:

```bash
ONLY_CAMPAIGN=YOUR_CAMPAIGN_ID scripts/collect_all_outbound_campaigns.sh
```

To resume starting at a particular campaign:

```bash
START_AT=YOUR_CAMPAIGN_ID scripts/collect_all_outbound_campaigns.sh
```

### Process and import all collected campaigns

Once the required `conversations.jsonl` files exist, run:

```bash
npm run load:all
```

This performs the documented project pipeline:

1. `sync:campaigns` — refresh `scripts/campaigns.json` from `outbound_campaigns.csv`.
2. `build:timings` — generate each campaign's `conversation_timings.csv`.
3. `import:campaigns` — import the timing CSVs into `data/outbound_dashboard.db`.

`npm run load:all` does **not** collect the Genesys conversations. Collection is a separate step.

---

## 13. Useful Project Commands

| Task | Command |
|---|---|
| Collect conversations | `npm run collect:conversations` |
| Sync campaign registry | `npm run sync:campaigns` |
| Build timing CSVs | `npm run build:timings` |
| Import campaigns | `npm run import:campaigns` |
| Full build/import | `npm run load:all` |
| List imported campaigns | `python3 scripts/import_campaigns.py --list` |
| Import all and skip missing CSVs | `python3 scripts/import_campaigns.py --all --skip-missing` |
| Recompute delay bands | `npm run recompute:delays` |

Optional diagnostic and chart-export tooling exists in the project but is not required for the standard conversation-data collection and dashboard import procedure.

---

## 14. Troubleshooting

### Analytics job returns no conversations

Verify:

- Campaign GUID
- Reporting interval
- Genesys Cloud region
- OAuth token
- Analytics permissions
- The campaign actually generated interactions during the interval

### `conversations.jsonl` is not created

Verify:

- `accessToken` is set
- `GC_REGION` is correct
- `curl` and `jq` are installed
- The Analytics `jobId` is correct
- The output directory exists/is writable

### Timing CSV is not created

Verify:

- `{CAMPAIGN_ID}/conversations.jsonl` exists
- The campaign exists in `scripts/campaigns.json`
- `dataDir` points to the correct location
- Python 3 is installed
- The campaign GUID supplied to `--campaign` is correct

### Campaign is missing from the dashboard

Check the SQLite import:

```bash
python3 scripts/import_campaigns.py --list
```

If the campaign is not imported, verify that `conversation_timings.csv` exists and rerun:

```bash
python3 scripts/import_campaigns.py --campaign YOUR_CAMPAIGN_ID
```

Then refresh the dashboard.

### Dashboard displays stale data

After importing:

- Click **Refresh Dashboard** in the Node-RED editor, or
- Redeploy the flow and hard-refresh the browser.

Also confirm that the correct campaign is selected in the Overview campaign dropdown.

### Import skips a campaign

Confirm that this file exists:

```text
{dataDir}/{CAMPAIGN_ID}/conversation_timings.csv
```

If it does not, run the timing build first.

---

## 15. Standard Single-Campaign Checklist

- [ ] Obtain the Genesys outbound campaign GUID.
- [ ] Define the reporting interval in UTC.
- [ ] Submit `POST /api/v2/analytics/conversations/details/jobs` using the campaign filter.
- [ ] Record the returned Analytics `jobId`.
- [ ] Set `accessToken`, `GC_REGION`, and `OUTPUT_MODE=ndjson`.
- [ ] Run `scripts/collect_genesys_conversations_access_token.sh`.
- [ ] Confirm `{dataDir}/{CAMPAIGN_ID}/conversations.jsonl` exists.
- [ ] If this is a new campaign, add it to `outbound_campaigns.csv` and run `npm run sync:campaigns`.
- [ ] Run `python3 scripts/build_all_campaign_timings.py --campaign YOUR_CAMPAIGN_ID`.
- [ ] Confirm `{dataDir}/{CAMPAIGN_ID}/conversation_timings.csv` exists.
- [ ] Run `python3 scripts/import_campaigns.py --campaign YOUR_CAMPAIGN_ID`.
- [ ] Run `python3 scripts/import_campaigns.py --list` and confirm the campaign is imported.
- [ ] Refresh the Node-RED dashboard.
- [ ] Select the campaign on Overview.
- [ ] Verify the campaign record count and dashboard metrics.
- [ ] Retain `conversations.jsonl` and `conversation_timings.csv` for traceability.

---

## 16. Expected End State

After a successful run, the relevant project data should be:

```text
data/campaigns/
└── {CAMPAIGN_ID}/
    ├── conversations.jsonl
    └── conversation_timings.csv

data/
└── outbound_dashboard.db
```

The relationship between the artifacts is:

- `conversations.jsonl` — raw Genesys conversation detail data.
- `conversation_timings.csv` — processed campaign timing/analysis data.
- `outbound_dashboard.db` — SQLite data source queried by the Node-RED dashboard.
- Node-RED Dashboard — visualization and analysis layer.

This separation should be maintained so dashboard results remain traceable to the source Genesys conversation data.
