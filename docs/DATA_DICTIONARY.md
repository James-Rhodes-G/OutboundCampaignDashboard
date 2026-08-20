# Outbound Dashboard — Data Dictionary

Database file: `data/outbound_dashboard.db` (SQLite, read-only from Node-RED)

## Pipeline overview

```
conversations.jsonl          Genesys Cloud Analytics Conversations Details export
        │
        ▼
genesys_delivery_analysis_speechfix_v4.py
        │  (one row per conversation)
        ▼
conversation_timings.csv
        │
        ▼
import_campaigns.py          adds campaign_id + derived compliance/time columns
        │
        ▼
conversations table          (+ campaigns registry table)
```

Optional side path:

```
Genesys diagnostics API  →  campaign_diagnostics/*.json  →  campaign_diagnostics table
```

### JSONL source shape

Each line in `conversations.jsonl` is a JSON object (or wrapped array) representing one **conversation**. The timing script expects Genesys Cloud **Conversations Details** shape:

| Top-level field | Aliases | Used for |
|-----------------|---------|----------|
| `conversationId` | `id` | `conversation_id` |
| `conversationStart` | `startTime` | `conversation_start` |
| `conversationEnd` | `endTime` | `conversation_end` |
| `participants[]` | — | All participant timing extraction |

Each **participant** may include:

| Participant field | Used for |
|-------------------|----------|
| `purpose`, `participantType` | Classify as customer / ivr / acd / agent |
| `participantName`, `name` | `*_participant_name` columns |
| `sessions[]` or `calls[]` | Voice session metrics, segments, disposition |
| Session `metrics[]` | e.g. `nConnected`, `nOffered`, `tAlert`, `nOutboundConnected` |
| Session `segments[]` | e.g. `dialing`, `ivr`, `delay`, `alert`, `interact` |
| Session `disposition` | Speech detection, analyzer, preconnect metadata |

Participant classification (`classify_participant`):

| Bucket | Rule (first match) |
|--------|---------------------|
| **customer** | `purpose` ∈ {customer, external, enduser} or `participantType` = external |
| **ivr** | `purpose` ∈ {ivr, flow} or `participantType` = ivr, or name contains outbound+flow |
| **acd** | `purpose` ∈ {acd, queue} or `participantType` = acd |
| **agent** | `purpose` ∈ {agent, user, internal} or `participantType` ∈ {agent, user} |
| **other** | Everything else |

When multiple participants match a bucket, the script picks the one with the **earliest** relevant timestamp for that stage.

---

## Table: `campaigns`

Registry of imported campaigns (one row per campaign). **Not sourced from JSONL** — written by `import_campaigns.py` at import time.

| Column | Type | Source / calculation |
|--------|------|----------------------|
| `id` | TEXT PK | Campaign identifier from `scripts/campaigns.json` (matches CSV / folder name). |
| `name` | TEXT | Display name from config (`id` with underscores replaced by spaces if synced from CSV). |
| `csv_path` | TEXT | Absolute path to the imported `conversation_timings.csv`. |
| `imported_at` | TEXT ISO-8601 UTC | Timestamp when import started. |
| `row_count` | INTEGER | Number of rows inserted into `conversations` for this campaign. |
| `file_size_bytes` | INTEGER | Size of the CSV file at import. |
| `file_mtime` | TEXT ISO-8601 UTC | CSV file modification time at import. |
| `genesys_guid` | TEXT | Genesys campaign GUID from config or updated from `campaign_diagnostics` import. |

---

## Table: `conversations`

One row per outbound conversation. Columns from CSV are produced by `genesys_delivery_analysis_speechfix_v4.py`; derived columns are added by `import_campaigns.py`.

### Import metadata

| Column | Type | JSONL / calculation |
|--------|------|---------------------|
| `id` | INTEGER PK | Auto-increment SQLite row id. |
| `campaign_id` | TEXT FK | Set at import from `campaigns.id` (not from JSONL). |
| `file` | TEXT | Source filename within the jsonl input (e.g. `conversations.jsonl`). |

### Conversation identity & duration

| Column | Type | JSONL / calculation |
|--------|------|---------------------|
| `conversation_id` | TEXT | `conversationId` or `id` on the conversation record. |
| `conversation_start` | TEXT ISO-8601 | `conversationStart` or `startTime`. |
| `conversation_end` | TEXT ISO-8601 | `conversationEnd` or `endTime`. |
| `conversation_duration_seconds` | REAL | `(conversation_end − conversation_start)` in seconds, rounded to 6 decimals. |

### Customer leg

Extracted from the **earliest-timestamp customer** participant (`purpose`/`participantType` = external/customer).

| Column | Type | JSONL / calculation |
|--------|------|---------------------|
| `customer_participant_name` | TEXT | Customer participant `participantName` or `name`. Often ANI/locality label (e.g. `Bradenton FL`), not a stripped identifier. |
| `customer_connected_time` | TEXT ISO-8601 | First available: session metric `nConnected.emitDate` → segment `dialing.segmentEnd` → session `connectedTime`. |
| `customer_connected_source` | TEXT | Which source won: `metric:nConnected`, `segment:dialing.end`, or `session.connectedTime`. |
| `dial_start_time` | TEXT ISO-8601 | Segment `dialing.segmentStart` → session `startTime`. |
| `dial_start_source` | TEXT | `segment:dialing.start` or `session.startTime`. |
| `preconnect_duration_seconds` | REAL | From disposition `dispositionParameters.adjustableLiveSpeakerDetection.preconnectDuration` (ISO-8601 duration `PTnS`) or session fields; parsed to seconds. |
| `total_ringbacks` | TEXT | From adjustable live speaker detection `totalRingbacks` (stored as string in CSV). |
| `line_connected` | TEXT | From adjustable live speaker detection `lineConnected` (`true`/`false` string). |
| `dial_to_customer_seconds` | REAL | `(customer_connected_time − dial_start_time)` seconds. |

### Speech / disposition detection

Found by **walking the entire conversation JSON tree** and taking the candidate with the **earliest** `detectedSpeechStart`.

| Column | Type | JSONL / calculation |
|--------|------|---------------------|
| `speech_detected_start` | TEXT ISO-8601 | `disposition.detectedSpeechStart` or `detectedSpeechStart` on any nested node. |
| `speech_detected_end` | TEXT ISO-8601 | Matching `detectedSpeechEnd` on the same winning candidate. |
| `disposition_analyzer` | TEXT | `disposition.dispositionAnalyzer` or `dispositionAnalyzer` (e.g. `speech.person`, `speech.machine`). |
| `disposition_name` | TEXT | Customer session disposition name if present; else from speech candidate `disposition.name` / `dispositionName`. |
| `speech_detected_participant_name` | TEXT | Participant label attached to the winning speech candidate. |
| `speech_detected_source` | TEXT | JSON path prefix where speech was found (e.g. `record.participants[0].sessions[0].disposition`). |
| `customer_to_speech_seconds` | REAL | `(speech_detected_start − customer_connected_time)` seconds. |

### IVR leg

From the **earliest-timestamp IVR** participant.

| Column | Type | JSONL / calculation |
|--------|------|---------------------|
| `ivr_participant_name` | TEXT | IVR participant `participantName` / `name`. |
| `ivr_start_time` | TEXT ISO-8601 | First available: segment `ivr.segmentStart` → metric `nFlow.emitDate` → session `connectedTime`. |
| `ivr_start_source` | TEXT | `segment:ivr.start`, `metric:nFlow`, or `session.connectedTime`. |
| `customer_to_ivr_seconds` | REAL | `(ivr_start_time − customer_connected_time)` seconds. |
| `speech_to_ivr_seconds` | REAL | `(ivr_start_time − speech_detected_start)` seconds. |

### ACD / queue leg

From the **earliest-timestamp ACD** participant.

| Column | Type | JSONL / calculation |
|--------|------|---------------------|
| `acd_participant_name` | TEXT | ACD participant `participantName` / `name`. |
| `acd_offer_time` | TEXT ISO-8601 | First available: metric `nOffered.emitDate` → segment `delay.segmentEnd` → segment `interact.segmentStart` → session `connectedTime`. |
| `acd_offer_source` | TEXT | `metric:nOffered`, `segment:delay.end`, `segment:interact.start`, or `session.connectedTime`. |
| `ivr_to_acd_seconds` | REAL | `(acd_offer_time − ivr_start_time)` seconds. |
| `customer_to_acd_seconds` | REAL | `(acd_offer_time − customer_connected_time)` seconds. |

### Agent leg

From the **earliest-timestamp agent** participant.

| Column | Type | JSONL / calculation |
|--------|------|---------------------|
| `agent_participant_name` | TEXT | Agent participant `participantName` / `name`. |
| `agent_alert_time` | TEXT ISO-8601 | First available: segment `alert.segmentStart` → session `startAlertingTime` → metric `tAlert.emitDate`. |
| `agent_alert_source` | TEXT | `segment:alert.start`, `session.startAlertingTime`, or `metric:tAlert`. |
| `agent_connected_time` | TEXT ISO-8601 | First available: metric `nOutboundConnected.emitDate` → segment `interact.segmentStart` → session `connectedTime`. |
| `agent_connected_source` | TEXT | `metric:nOutboundConnected`, `segment:interact.start`, or `session.connectedTime`. |
| `acd_to_alert_seconds` | REAL | `(agent_alert_time − acd_offer_time)` seconds. |
| `alert_to_agent_seconds` | REAL | `(agent_connected_time − agent_alert_time)` seconds. |
| `customer_to_agent_seconds` | REAL | `(agent_connected_time − customer_connected_time)` seconds. End-to-end customer connect → agent connect. |

### Latency summary (from CSV)

| Column | Type | JSONL / calculation |
|--------|------|---------------------|
| `dominant_latency_stage` | TEXT | Name of the **largest** stage duration among: `dial_to_customer_seconds`, `customer_to_speech_seconds`, `customer_to_ivr_seconds`, `speech_to_ivr_seconds`, `ivr_to_acd_seconds`, `acd_to_alert_seconds`, `alert_to_agent_seconds`. Ties go to the last stage with the max value in that ordered list. |
| `dominant_latency_seconds` | REAL | Value of the winning stage duration. |
| `has_conference` | TEXT | `true` if any participant session has a segment with `conference: true`; otherwise `false`. |
| `notes` | TEXT | Reserved; always empty from the timing script today. |

### Derived at import (`import_campaigns.py`)

These columns are **not** in the CSV; they are computed when rows are loaded into SQLite.

| Column | Type | Calculation |
|--------|------|-------------|
| `delay_seconds` | REAL | **Compliance delay** (primary dashboard metric anchor):<br>• **Agent connected:** `(agent_connected_time − speech_detected_end)` in seconds.<br>• **Queue abandon:** if `speech_detected_end` and `acd_offer_time` exist but `agent_connected_time` is empty, `(conversation_end − speech_detected_end)`.<br>Negative values are stored as NULL. |
| `delay_band` | TEXT | Bucket for `delay_seconds`: `0-2` (&lt;2s), `2-10` (&lt;10s), `10-20` (&lt;20s), `20+` (≥20s). NULL if `delay_seconds` is NULL. |
| `hour` | INTEGER 0–23 | Hour from `conversation_start` (local parsing of ISO timestamp). |
| `weekday` | TEXT | Three-letter weekday from `conversation_start` (`Mon` … `Sun`). |
| `date` | TEXT ISO date | Date portion of `conversation_start` (`YYYY-MM-DD`). |

### Dashboard-only metrics (not stored as columns)

The Node-RED flow computes additional values in SQL at query time using the same anchors:

| Metric | SQL logic (simplified) |
|--------|------------------------|
| **Disposition→Agent** | `(agent_connected_time − speech_detected_end)` when both timestamps exist. |
| **Disposition→Abandon** | `(conversation_end − speech_detected_end)` when offered to queue (`acd_offer_time`) but no agent. |
| **Connected duration** | `(conversation_end − customer_connected_time)` for answered calls. |

These align with `delay_seconds` for compliance paths but may appear under different names in chart labels.

---

## Table: `campaign_diagnostics` (optional)

Populated only if `import_campaign_diagnostics.py` is run. **Not derived from JSONL.**

| Column | Type | Source |
|--------|------|--------|
| `campaign_id` | TEXT PK | From diagnostic JSON `campaign_id` or filename stem. |
| `genesys_guid` | TEXT | Genesys campaign GUID from API export. |
| `fetched_at` | TEXT | When the diagnostic summary was fetched. |
| `health_state` | TEXT | Genesys outbound diagnostics health state. |
| `campaign_state` | TEXT | Genesys campaign run state. |
| `summary_json` | TEXT | Full API summary object as JSON string. |

---

## Segment & metric reference (JSONL)

Common Genesys session **metrics** used for timestamps:

| Metric name | Typical meaning | Used for |
|-------------|-----------------|----------|
| `nConnected` | Customer/far-end connected | `customer_connected_time` |
| `nFlow` | Flow/IVR entered | `ivr_start_time` |
| `nOffered` | Offered to ACD | `acd_offer_time` |
| `tAlert` | Agent alerting | `agent_alert_time` |
| `nOutboundConnected` | Agent connected on outbound | `agent_connected_time` |

Common session **segment types**:

| segmentType | Field read | Used for |
|-------------|------------|----------|
| `dialing` | `segmentStart` / `segmentEnd` | Dial start / customer answer |
| `ivr` | `segmentStart` | IVR start |
| `delay` | `segmentEnd` | ACD offer (fallback) |
| `alert` | `segmentStart` | Agent alert |
| `interact` | `segmentStart` | Agent connect / ACD offer (fallback) |

---

## Data quality notes

1. **Null timestamps** — If a stage did not occur (e.g. no agent on a no-answer call), related seconds columns are NULL.
2. **Source columns** — `*_source` fields document which JSON path won when multiple candidates exist; useful for debugging timing chain gaps.
3. **Not anonymized** — `conversation_id`, participant names/locality strings, and precise timestamps remain in the database.
4. **Re-import** — Running `import_campaigns.py` for a campaign **deletes and replaces** all `conversations` rows for that `campaign_id`.
5. **Recompute** — `import_campaigns.py --recompute-delays` recalculates `delay_seconds` / `delay_band` / time dimensions from stored timestamps without re-reading JSONL.

---

## Related files

| File | Role |
|------|------|
| `scripts/genesys_delivery_analysis_speechfix_v4.py` | JSONL → CSV extraction logic (authoritative for CSV columns) |
| `scripts/import_campaigns.py` | CSV → SQLite + derived columns |
| `scripts/import_campaign_diagnostics.py` | Optional diagnostics JSON → SQLite |
| `OutboundDashboard.json` | Dashboard SQL queries over `conversations` |
