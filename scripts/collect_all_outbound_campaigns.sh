#!/usr/bin/env bash
set -euo pipefail

# Fetch Genesys analytics job results for every row in outbound_campaigns.csv.
# Writes {dataDir}/{CAMPAIGN_ID}/conversations.jsonl per campaign.
#
# Usage:
#   export accessToken='...'
#   ./scripts/collect_all_outbound_campaigns.sh
#
# Optional environment variables:
#   OUTBOUND_DATA_DIR  Override dataDir (default: scripts/campaigns.json → dataDir)
#   GC_REGION          Default: usw2.pure.cloud
#   OUTPUT_MODE        Default: ndjson
#   SKIP_EXISTING      Default: 1 (skip if campaign dir already has conversations.jsonl)
#   START_AT           Optional campaign name to resume from
#   ONLY_CAMPAIGN      Optional single campaign name to collect

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COLLECT_SCRIPT="${SCRIPT_DIR}/collect_genesys_conversations_access_token.sh"
CONFIG_FILE="${SCRIPT_DIR}/campaigns.json"
OUTPUT_NAME="conversations.jsonl"

: "${accessToken:?ERROR: export accessToken before running}"
GC_REGION="${GC_REGION:-usw2.pure.cloud}"
OUTPUT_MODE="${OUTPUT_MODE:-ndjson}"
SKIP_EXISTING="${SKIP_EXISTING:-1}"

export accessToken GC_REGION OUTPUT_MODE

if [[ -n "${OUTBOUND_DATA_DIR:-}" ]]; then
  DATA_DIR="$OUTBOUND_DATA_DIR"
elif [[ -f "$CONFIG_FILE" ]]; then
  DATA_DIR="$(python3 -c "import sys; sys.path.insert(0, '${SCRIPT_DIR}'); from project_paths import resolve_data_dir; print(resolve_data_dir())")"
else
  echo "ERROR: set OUTBOUND_DATA_DIR or create scripts/campaigns.json" >&2
  exit 1
fi

DATA_DIR="${DATA_DIR/#\~/$HOME}"
if [[ "$DATA_DIR" != /* ]]; then
  DATA_DIR="$(cd "${SCRIPT_DIR}/.." && cd "$DATA_DIR" && pwd)"
fi
CSV_FILE="${DATA_DIR}/outbound_campaigns.csv"

if [[ ! -f "$CSV_FILE" ]]; then
  echo "ERROR: CSV not found: $CSV_FILE" >&2
  exit 1
fi

if [[ ! -x "$COLLECT_SCRIPT" ]]; then
  chmod +x "$COLLECT_SCRIPT"
fi

strip_field() {
  local value="$1"
  value="${value//$'\r'/}"
  value="${value#"${value%%[![:space:]]*}"}"
  value="${value%"${value##*[![:space:]]}"}"
  printf '%s' "$value"
}

started="${START_AT:-}"
only="${ONLY_CAMPAIGN:-}"
resume=0
if [[ -n "$started" ]]; then
  resume=1
fi

while IFS=',' read -r name _guid _status jobId || [[ -n "${name:-}" ]]; do
  name="$(strip_field "$name")"
  jobId="$(strip_field "$jobId")"
  [[ "$name" == "name" ]] && continue
  [[ -z "$name" || -z "$jobId" ]] && continue

  if [[ -n "$only" && "$name" != "$only" ]]; then
    continue
  fi

  if [[ "$resume" -eq 1 ]]; then
    if [[ "$name" == "$started" ]]; then
      resume=0
    else
      echo "Skipping ${name} (before START_AT=${started})" >&2
      continue
    fi
  fi

  campaign_dir="${DATA_DIR}/${name}"
  target_file="${campaign_dir}/${OUTPUT_NAME}"
  temp_file="${DATA_DIR}/${OUTPUT_NAME}"

  mkdir -p "$campaign_dir"

  if [[ "$SKIP_EXISTING" == "1" && -s "$target_file" ]]; then
    echo "SKIP: ${name} already has ${OUTPUT_NAME}" >&2
    continue
  fi

  echo "=== Collecting ${name} (jobId=${jobId}) ===" >&2
  rm -f "$temp_file"

  if ! "$COLLECT_SCRIPT" "$jobId" "$temp_file"; then
    echo "ERROR: collection failed for ${name}" >&2
    exit 1
  fi

  mv "$temp_file" "$target_file"
  echo "Moved ${OUTPUT_NAME} -> ${target_file}" >&2
done < "$CSV_FILE"

echo "All requested campaigns collected." >&2
