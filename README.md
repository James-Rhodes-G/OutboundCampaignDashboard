# Outbound Analytics Dashboard

Node-RED Dashboard 2.0 application for analyzing Genesys Cloud outbound campaign conversations. Data is stored in SQLite and queried on demand — the dashboard scales to campaigns with millions of interactions.

**Dashboard URL (local):** `http://127.0.0.1:1880/dashboard/overview`

**GitHub:** `git@github.com:James-Rhodes-G/OutboundCampaignDashboard.git`

---

## Prerequisites

| Requirement | Notes |
|-------------|-------|
| **Node-RED** | With Dashboard 2.0 (`@flowfuse/node-red-dashboard`) |
| **Node.js / npm** | For project dependencies |
| **Python 3** | For data import scripts |
| **Data directory** | Campaign CSV and `conversations.jsonl` per campaign (see [Data pipeline](#data-pipeline)) |
| **curl / jq** | Required for Genesys conversation export scripts |
| **Playwright** (optional) | Only for bulk PNG export — install browsers after `npm install` |

Install Node-RED dependencies from the project root:

```bash
npm install
npx playwright install chromium   # only if using bulk chart export
```

The SQLite database lives at `data/outbound_dashboard.db` (gitignored). Create it by running the import scripts below.

---

## Project structure

```
OutboundDashboard/
├── OutboundDashboard.json    # Node-RED flow (dashboard + SQL queries)
├── package.json                # npm scripts for data loading and export
├── data/
│   └── outbound_dashboard.db   # SQLite database (created by import)
└── scripts/                    # all pipeline tooling lives here
    ├── project_paths.py        # shared paths + dataDir resolution
    ├── campaigns.example.json  # Template — copy to campaigns.json (gitignored)
    ├── campaigns.json          # Local campaign registry (gitignored)
    ├── collect_genesys_conversations_access_token.sh  # Genesys analytics job → jsonl
    ├── collect_all_outbound_campaigns.sh              # bulk fetch from outbound_campaigns.csv
    ├── genesys_delivery_analysis_speechfix_v4.py      # jsonl → conversation_timings.csv
    ├── sync_campaigns_config.py
    ├── build_all_campaign_timings.py
    ├── import_campaigns.py
    ├── export_campaign_charts.mjs      # Bulk PNG export via Playwright
    ├── fetch_campaign_diagnostics.py   # optional — Genesys API
    ├── fetch_campaign_whole_agents.py  # optional — Genesys API
    └── import_campaign_diagnostics.py  # optional
```

External data directory (configured in `scripts/campaigns.json` → `dataDir`; default `data/campaigns`):

```
data/campaigns/                 # or any path you set in campaigns.json
├── outbound_campaigns.csv
├── {CAMPAIGN_ID}/
│   ├── conversations.jsonl
│   └── conversation_timings.csv    # built by build_all_campaign_timings.py
├── campaign_diagnostics/           # optional JSON from Genesys API
└── campaign_whole_agents/            # optional JSON from whole-agent fetch
```

---

## Quick start

1. **Clone and install**

   ```bash
   git clone git@github.com:James-Rhodes-G/OutboundCampaignDashboard.git
   cd OutboundCampaignDashboard
   npm install
   cp scripts/campaigns.example.json scripts/campaigns.json
   ```

2. **Configure data path** — edit `dataDir` in `scripts/campaigns.json` (default: `data/campaigns`), place `outbound_campaigns.csv` there, then run `npm run sync:campaigns`.

3. **Deploy the flow** in Node-RED (or restart Node-RED if the project auto-loads).

4. **Load data** (first time or after new exports):

   ```bash
   npm run load:all
   ```

5. Open **`http://127.0.0.1:1880/dashboard/overview`**
6. Select a campaign from the **Campaign** dropdown on Overview.
7. Click **Refresh Dashboard** (inject on the Dashboard tab in the editor) if widgets do not update after import.

---

## Using the dashboard

### Campaign selection

- The **Campaign** dropdown on **Overview** sets the active campaign for the entire dashboard.
- The header chip shows the selected campaign name and record count.
- Options are loaded from the SQLite `campaigns` table (refreshed on deploy / page load).

### Refresh after data import

After running import scripts, trigger a dashboard refresh:

- In the Node-RED editor, open the **Dashboard** tab and click the **Refresh Dashboard** inject node, **or**
- Redeploy the flow and hard-refresh the browser (`Cmd+Shift+R`).

### Primary latency metric

Most charts and KPIs use **Disposition→Agent**: seconds from `speech_detected_end` to `agent_connected_time` on agent-connected calls. Compliance bands use **0–2**, **2–10**, **10–20**, and **20+** seconds. See **Metric Definitions** in the dashboard for full formulas.

---

## Dashboard pages

| Page | Path | Purpose |
|------|------|---------|
| **Overview** | `/overview` | Campaign selector, KPI tiles, health charts, disposition summary |
| **Contact Analysis** | `/contact-analysis` | Outcomes, funnel, Sankey flow, routing metrics, customer proof |
| **Latency Analysis** | `/latency-analysis` | Connect timeline, disposition latency, delay bands, slowest calls, outliers |
| **Routing Analysis** | `/routing-analysis` | Agent routing and stage timing |
| **Time Analysis** | `/time-analysis` | Disposition→Agent by hour, weekday, and date |
| **Root Cause Analysis** | `/root-cause-analysis` | Worst-call investigation tables |
| **Conversation Explorer** | `/conversation-explorer` | Row-level search and raw field inspection (latest 25 rows) |
| **Data Quality** | `/data-quality` | Completeness and timing-chain coverage |
| **Metric Definitions** | `/metric-definitions` | Plain-English definitions for every metric |

### Overview tabs

- **Campaign** — campaign dropdown
- **KPIs** — latency, contact effectiveness, routing, compliance summary cards
- **Campaign Health** — compliance bands, daily volume, interaction counts, disposition→agent/abandon trends, top dispositions
- **Disposition Summary** — outcome counts table

### Contact Analysis tabs

- **Contact Outcomes** — disposition mix (live voice, machine, no answer, etc.)
- **Contact Funnel** — stage progression (Attempts → Connected → Live Voice / Call Screener paths)
- **Contact Flow** — Sankey diagram and high-value KPI table
- **Agent & Routing Contact Metrics** — IVR/ACD/agent stage counts
- **Customer Proof** — reconciliation tables for customer-facing reports

### Latency Analysis tabs

- **Connect Timeline** — KPI headline, boundary alignment map, segment definitions (with P90), PNG export buttons
- **Timeline Details** — pre-disposition lead-in bar, post-disposition ECharts chart, milestones, legend
- Additional tabs on this page: delay bands, slowest calls, outliers, latency summary

### Exporting charts for reports (PNG)

Several views include **Download PNG for report** buttons. Files are named `{CAMPAIGN_ID}_{chart-slug}_{date}.png`.

| Location | Exports |
|----------|---------|
| **Overview → Campaign Health** | Export panel: compliance bands, daily call volume, interaction counts, disposition→agent, disposition→abandon, top dispositions |
| **Contact Analysis → Contact Flow** | Contact flow Sankey |
| **Contact Analysis → Contact Funnel** | Full funnel graphic |
| **Latency Analysis → Connect Timeline** | Boundary chart PNG and full customer view PNG (headline + map + definitions) |

Insert PNGs into Word via **Insert → Pictures**. Charts render at 2× resolution for crisp print quality.

The **Funnel Stage Detail** table (Contact Funnel tab) can be copied directly into Excel for tabular funnel data.

#### Bulk export all campaigns

With Node-RED running and the dashboard reachable, export PNGs for every campaign in SQLite:

```bash
# All dashboard charts (health, sankey, connect timeline)
npm run export:charts

# Connect timeline only (chart + full view per campaign)
npm run export:timeline

# Force re-export even if today's files exist
node scripts/export_campaign_charts.mjs --timeline --force
```

Output directory defaults to `exports/charts` in the project root. Override with:

```bash
export OUTBOUND_CHARTS_DIR="/path/to/Charts"
export OUTBOUND_DASHBOARD_URL="http://127.0.0.1:1880/dashboard"
npm run export:timeline
```

---

## Data pipeline

### Fetching conversations from Genesys (optional first step)

If you do not already have `{CAMPAIGN_ID}/conversations.jsonl` under your data directory, export them from Genesys Cloud analytics jobs using the scripts in `scripts/`.

**Single campaign or job:**

```bash
export accessToken="your-oauth-token"
export GC_REGION="usw2.pure.cloud"   # or your org region
export OUTPUT_MODE=ndjson            # recommended for jsonl output

# By analytics job ID (from outbound_campaigns.csv)
scripts/collect_genesys_conversations_access_token.sh \
  "<job-id-uuid>" \
  "data/campaigns/YOUR_CAMPAIGN_ID/conversations.jsonl"

# Or by conversation ID list file
scripts/collect_genesys_conversations_access_token.sh \
  conversation_ids.txt \
  conversations.jsonl
```

**All campaigns in `outbound_campaigns.csv`:**

```bash
export accessToken="your-oauth-token"
export GC_REGION="usw2.pure.cloud"

# Uses scripts/campaigns.json → dataDir for CSV and output folders
scripts/collect_all_outbound_campaigns.sh

# Optional: resume or limit scope
ONLY_CAMPAIGN=YOUR_CAMPAIGN_ID scripts/collect_all_outbound_campaigns.sh
START_AT=YOUR_CAMPAIGN_ID scripts/collect_all_outbound_campaigns.sh
```

Requires `curl` and `jq`. Tokens are never stored in the repo — pass `accessToken` in the environment only.

### End-to-end: all campaigns

```bash
npm run load:all
```

This runs:

1. `sync:campaigns` — update `scripts/campaigns.json` from `outbound_campaigns.csv`
2. `build:timings` — run `scripts/genesys_delivery_analysis_speechfix_v4.py` per campaign to produce `conversation_timings.csv`
3. `import:campaigns` — stream CSVs into `data/outbound_dashboard.db`

### With Genesys campaign diagnostics (optional)

Requires a Genesys OAuth `accessToken`:

```bash
export accessToken="your-token"
export GC_REGION="usw2.pure.cloud"   # if not default

npm run load:all-with-diagnostics
```

### npm scripts reference

| Script | Command | Description |
|--------|---------|-------------|
| Collect conversations | `npm run collect:conversations` | Fetch all `conversations.jsonl` from Genesys (needs `accessToken`) |
| Sync campaign list | `npm run sync:campaigns` | Refresh `campaigns.json` from CSV |
| Build timing CSVs | `npm run build:timings` | Generate `conversation_timings.csv` from jsonl |
| Import conversations | `npm run import:campaigns` | Load timing CSVs into SQLite |
| Fetch diagnostics | `npm run fetch:diagnostics` | Pull Genesys diagnostic summaries |
| Fetch whole agents | `npm run fetch:whole-agents` | Pull Genesys queue membership / whole-agent counts |
| Import diagnostics | `npm run import:diagnostics` | Load diagnostics JSON into SQLite |
| Recompute delay bands | `npm run recompute:delays` | Re-derive `delay_seconds` / `delay_band` without re-importing CSVs |
| Export all chart PNGs | `npm run export:charts` | Playwright bulk export for all campaigns |
| Export connect timeline | `npm run export:timeline` | Connect timeline chart + full view only |
| Full load | `npm run load:all` | Sync + build + import |
| Full load + diagnostics | `npm run load:all-with-diagnostics` | Above plus Genesys diagnostics |

### Adding a new campaign

1. Add the campaign to **`outbound_campaigns.csv`** in your data directory (name + Genesys GUID).
2. Collect conversation data into **`{CAMPAIGN_ID}/conversations.jsonl`** (see [Fetching conversations from Genesys](#fetching-conversations-from-genesys-optional-first-step)).
3. Sync and import:

   ```bash
   npm run sync:campaigns
   python3 scripts/build_all_campaign_timings.py --campaign NEW_CAMPAIGN_ID
   python3 scripts/import_campaigns.py --campaign NEW_CAMPAIGN_ID
   ```

   Or use `npm run load:all` to process every configured campaign (`--skip-missing` skips campaigns without jsonl/csv).

4. Refresh the dashboard in Node-RED. The new campaign appears in the dropdown automatically.

### Updating existing campaign data

Replace `conversations.jsonl` with fresh exports, then:

```bash
python3 scripts/build_all_campaign_timings.py --campaign YOUR_CAMPAIGN_ID
python3 scripts/import_campaigns.py --campaign YOUR_CAMPAIGN_ID
```

Re-import replaces all rows for that campaign in SQLite.

### Useful import flags

```bash
# List configured and imported campaigns
python3 scripts/import_campaigns.py --list

# Import all campaigns, skip any missing CSV
python3 scripts/import_campaigns.py --all --skip-missing

# Recompute compliance delay bands after logic changes (no CSV re-read)
python3 scripts/import_campaigns.py --recompute-delays --all
```

### Configuring the data directory

Edit `dataDir` in `scripts/campaigns.json` if campaign data lives outside the project (default: `data/campaigns`):

```json
{
  "dataDir": "data/campaigns",
  "campaigns": [ ... ]
}
```

---

## Troubleshooting

| Issue | Fix |
|-------|-----|
| Empty dashboard / no campaigns in dropdown | Run `npm run import:campaigns` and click **Refresh Dashboard** |
| Campaign missing from dropdown | Confirm it was imported: `python3 scripts/import_campaigns.py --list` |
| Charts show stale data | Refresh dashboard after import; confirm correct campaign selected |
| Import skips a campaign | Ensure `{CAMPAIGN_ID}/conversation_timings.csv` exists (run `build:timings` first) |
| Large campaign slow to load | Expected for multi-million row campaigns; wait for queries to complete after switching campaigns |
| PNG export says "chart not ready" | Wait for data to load on the active tab, then retry |
| Bulk export fails on Playwright | Run `npx playwright install chromium` after `npm install` |
| Connect Timeline tab layout wrong after switching tabs | Hard-refresh browser; redeploy latest flow |

---

## Development notes

- Flow file: `OutboundDashboard.json` — all SQL, formatters, and Dashboard 2 widgets.
- One-time dashboard patch scripts were removed; the flow JSON is the source of truth for UI behavior.
- Database schema is created automatically by `import_campaigns.py` on first run.
- Gitignored locally: `data/`, `*.db`, `node_modules/`, `.playwright-browsers/`, `OutboundDashboard_cred.json`, `scripts/campaigns.json`, `exports/`, `*.backup`.
- `OutboundDashboard_cred.json` holds Node-RED credentials — keep it local, never commit secrets.

### Pushing to GitHub

```bash
git add .gitignore README.md package.json package-lock.json OutboundDashboard.json scripts/
git rm --cached OutboundDashboard_cred.json scripts/campaigns.json 2>/dev/null || true
git commit -m "Outbound dashboard flow, scripts, and documentation"
git push origin main
```

After cloning on another machine: `npm install`, `cp scripts/campaigns.example.json scripts/campaigns.json`, edit `dataDir`, run `npm run load:all`, deploy the flow in Node-RED.
