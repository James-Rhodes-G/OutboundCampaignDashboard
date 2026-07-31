#!/usr/bin/env bash
set -euo pipefail

# Collect Genesys Cloud conversation details using an existing access token.
#
# Required environment variables:
#   accessToken   Genesys Cloud OAuth access token
#
# Optional environment variables:
#   GC_API_HOST   API host, e.g. api.mypurecloud.com or api.usw2.pure.cloud
#   GC_REGION     Alternative to GC_API_HOST, e.g. usw2.pure.cloud (script uses api.${GC_REGION})
#   BATCH_SIZE    Number of conversation IDs per request (default: 50)
#   PAGE_SIZE     Job results page size (default: 999)
#   OUTPUT_MODE   array (default) or ndjson
#   MAX_RETRIES   Retry attempts for transient failures (default: 5)
#   INITIAL_BACKOFF Seconds for the first retry backoff (default: 1)
#   JOB_POLL_SECONDS Seconds between job status checks (default: 5)
#
# Usage:
#   ./collect_genesys_conversations_access_token.sh <jobId> conversations.jsonl
#   ./collect_genesys_conversations_access_token.sh conversation_ids.txt conversations.json

usage() {
  cat >&2 <<'EOF_USAGE'
Usage: collect_genesys_conversations_access_token.sh <jobId|conversation_id_file> <output_file>

Environment:
  accessToken   Required Genesys Cloud access token
  GC_API_HOST   Optional API host, e.g. api.mypurecloud.com
  GC_REGION     Optional region suffix, e.g. usw2.pure.cloud
  BATCH_SIZE    Optional batch size per request (default 50)
  PAGE_SIZE     Optional job results page size (default 999)
  OUTPUT_MODE   Optional: array (default) or ndjson
  MAX_RETRIES   Optional retry attempts for 429/5xx (default 5)
  INITIAL_BACKOFF Optional initial retry backoff in seconds (default 1)
  JOB_POLL_SECONDS Optional job status poll interval in seconds (default 5)
EOF_USAGE
}

if [[ $# -lt 2 ]]; then
  usage
  exit 1
fi

INPUT="${1//$'\r'/}"
INPUT="${INPUT#"${INPUT%%[![:space:]]*}"}"
INPUT="${INPUT%"${INPUT##*[![:space:]]}"}"
OUT_FILE="${2//$'\r'/}"
OUTPUT_MODE="${OUTPUT_MODE:-array}"
BATCH_SIZE="${BATCH_SIZE:-50}"
PAGE_SIZE="${PAGE_SIZE:-999}"
MAX_RETRIES="${MAX_RETRIES:-5}"
INITIAL_BACKOFF="${INITIAL_BACKOFF:-1}"
JOB_POLL_SECONDS="${JOB_POLL_SECONDS:-5}"

: "${accessToken:?ERROR: accessToken environment variable is not set}"

if [[ -n "${GC_API_HOST:-}" ]]; then
  API_HOST="$GC_API_HOST"
elif [[ -n "${GC_REGION:-}" ]]; then
  API_HOST="api.${GC_REGION}"
else
  API_HOST="api.mypurecloud.com"
fi

API_BASE="https://${API_HOST}/api/v2/analytics/conversations/details"
FAIL_FILE="${OUT_FILE}.failed_ids.txt"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

command -v curl >/dev/null 2>&1 || { echo "ERROR: curl is required" >&2; exit 1; }
command -v jq >/dev/null 2>&1 || { echo "ERROR: jq is required" >&2; exit 1; }

case "$OUTPUT_MODE" in
  array|ndjson) ;;
  *) echo "ERROR: OUTPUT_MODE must be array or ndjson" >&2; exit 1 ;;
esac

UUID_RE='^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'
JOB_MODE=0
IDS_FILE=""

if [[ "$INPUT" =~ $UUID_RE ]]; then
  JOB_MODE=1
  JOB_ID="$INPUT"
elif [[ -f "$INPUT" ]]; then
  JOB_MODE=0
  IDS_FILE="$INPUT"
else
  echo "ERROR: input must be a jobId UUID or an existing conversation ID file: $INPUT" >&2
  exit 1
fi

RAW_NDJSON="${TMP_DIR}/raw.ndjson"
: > "$RAW_NDJSON"
: > "$FAIL_FILE"

curl_json() {
  local url="$1" attempt=1 backoff="$INITIAL_BACKOFF"
  while true; do
    local response_file="${TMP_DIR}/response_${RANDOM}_${attempt}.json" http_code
    http_code="$(curl -sS -o "$response_file" -w '%{http_code}' \
      -H "Authorization: Bearer ${accessToken}" -H 'Accept: application/json' "$url" || true)"
    if [[ "$http_code" == "200" ]]; then cat "$response_file"; return 0; fi
    if [[ "$http_code" == "401" ]]; then echo "ERROR: invalid or expired token (HTTP 401)." >&2; return 2; fi
    if [[ "$http_code" == "429" || "$http_code" =~ ^5[0-9][0-9]$ ]]; then
      if [[ "$attempt" -ge "$MAX_RETRIES" ]]; then
        echo "ERROR: request failed after ${MAX_RETRIES} attempts (HTTP ${http_code})." >&2
        cat "$response_file" >&2 || true; return 1
      fi
      echo "WARN: HTTP ${http_code}; retry in ${backoff}s (${attempt}/${MAX_RETRIES})." >&2
      sleep "$backoff"
      attempt=$((attempt + 1))
      backoff="$(awk -v b="$backoff" 'BEGIN { printf "%.3f", (b * 2) }')"
      continue
    fi
    echo "ERROR: request failed with HTTP ${http_code:-unknown}." >&2
    cat "$response_file" >&2 || true; return 1
  done
}

extract_conversations() {
  jq -c '
    def emit:
      if type == "array" then .[]
      elif type == "object" then
        if (.conversations? | type) == "array" then .conversations[]
        elif (.entities? | type) == "array" then .entities[]
        elif (.results? | type) == "array" then .results[]
        elif (.items? | type) == "array" then .items[]
        elif (.data? | type) == "array" then .data[]
        else . end
      else empty end;
    emit'
}

wait_for_job() {
  local job_id="$1" status state payload
  while true; do
    payload="$(curl_json "${API_BASE}/jobs/${job_id}")" || return $?
    status="$(printf '%s' "$payload" | jq -r '.status // empty')"
    state="$(printf '%s' "$payload" | jq -r '.state // empty')"
    echo "Job ${job_id} status=${status:-unknown} state=${state:-unknown}" >&2
    status_lc="$(printf '%s' "$status" | tr '[:upper:]' '[:lower:]')"
    state_lc="$(printf '%s' "$state" | tr '[:upper:]' '[:lower:]')"
    case "$status_lc" in
      fulfilled|complete|completed) return 0 ;;
      failed|cancelled|canceled|expired)
        echo "ERROR: job ${job_id} ended with status ${status}." >&2; return 1 ;;
    esac
    case "$state_lc" in
      fulfilled|complete|completed) return 0 ;;
      failed|cancelled|canceled|expired)
        echo "ERROR: job ${job_id} ended with state ${state}." >&2; return 1 ;;
    esac
    sleep "$JOB_POLL_SECONDS"
  done
}

collect_from_job() {
  local job_id="$1" cursor="" page=1 total=0 url payload page_count
  echo "Waiting for analytics job ${job_id}..." >&2
  wait_for_job "$job_id" || return $?
  while true; do
    if [[ -z "$cursor" ]]; then
      url="${API_BASE}/jobs/${job_id}/results?pageSize=${PAGE_SIZE}"
    else
      url="${API_BASE}/jobs/${job_id}/results?pageSize=${PAGE_SIZE}&cursor=$(printf '%s' "$cursor" | jq -sRr @uri)"
    fi
    echo "Fetching job results page ${page}..." >&2
    payload="$(curl_json "$url")" || return $?
    page_count="$(printf '%s' "$payload" | jq '.conversations | length')"
    if [[ "$page_count" -gt 0 ]]; then
      printf '%s' "$payload" | jq -c '.conversations[]' >> "$RAW_NDJSON"
      total=$((total + page_count))
      echo "Collected ${total} conversations so far." >&2
    fi
    cursor="$(printf '%s' "$payload" | jq -r '.cursor // empty')"
    [[ -z "$cursor" ]] && break
    page=$((page + 1))
  done
}

request_batch() {
  local ids_csv="$1" attempt=1 backoff="$INITIAL_BACKOFF"
  while true; do
    local response_file="${TMP_DIR}/response_${RANDOM}_${attempt}.json" http_code
    http_code="$(curl -sS -o "$response_file" -w '%{http_code}' \
      -H "Authorization: Bearer ${accessToken}" -H 'Accept: application/json' \
      "${API_BASE}?id=${ids_csv}" || true)"
    if [[ "$http_code" == "200" ]]; then cat "$response_file"; return 0; fi
    if [[ "$http_code" == "401" ]]; then echo "ERROR: invalid or expired token (HTTP 401)." >&2; return 2; fi
    if [[ "$http_code" == "429" || "$http_code" =~ ^5[0-9][0-9]$ ]]; then
      if [[ "$attempt" -ge "$MAX_RETRIES" ]]; then
        echo "ERROR: batch failed after ${MAX_RETRIES} attempts (HTTP ${http_code})." >&2
        cat "$response_file" >&2 || true; return 1
      fi
      sleep "$backoff"
      attempt=$((attempt + 1))
      backoff="$(awk -v b="$backoff" 'BEGIN { printf "%.3f", (b * 2) }')"
      continue
    fi
    echo "ERROR: batch failed with HTTP ${http_code:-unknown}." >&2
    cat "$response_file" >&2 || true; return 1
  done
}

if [[ "$JOB_MODE" -eq 1 ]]; then
  collect_from_job "$JOB_ID" || exit $?
else
  mapfile -t conversation_ids < <(awk '/^[[:space:]]*#/ {next} /^[[:space:]]*$/ {next} {gsub(/^[[:space:]]+|[[:space:]]+$/, ""); if (length($0) > 0) print}' "$IDS_FILE")
  [[ ${#conversation_ids[@]} -gt 0 ]] || { echo "ERROR: no conversation IDs in $IDS_FILE" >&2; exit 1; }
  processed=0
  batch_count=0
  batch_total=$(( (${#conversation_ids[@]} + BATCH_SIZE - 1) / BATCH_SIZE ))
  for ((i = 0; i < ${#conversation_ids[@]}; i += BATCH_SIZE)); do
    batch_count=$((batch_count + 1))
    batch=("${conversation_ids[@]:i:BATCH_SIZE}")
    ids_csv="$(IFS=,; printf '%s' "${batch[*]}")"
    echo "Fetching batch ${batch_count}/${batch_total} (${#batch[@]} IDs)..." >&2
    if response_json="$(request_batch "$ids_csv")"; then
      printf '%s' "$response_json" | extract_conversations >> "$RAW_NDJSON" || printf '%s\n' "${batch[@]}" >> "$FAIL_FILE"
      processed=$((processed + ${#batch[@]}))
      echo "Processed ${processed}/${#conversation_ids[@]} conversation IDs." >&2
    else
      rc=$?; [[ "$rc" -eq 2 ]] && exit 2
      printf '%s\n' "${batch[@]}" >> "$FAIL_FILE"
    fi
  done
fi

case "$OUTPUT_MODE" in
  ndjson) cp "$RAW_NDJSON" "$OUT_FILE" ;;
  array) jq -s '.' "$RAW_NDJSON" > "$OUT_FILE" ;;
esac

if [[ "$JOB_MODE" -eq 0 && -s "$FAIL_FILE" ]]; then
  echo "Completed with failures. Failed IDs: $FAIL_FILE" >&2
else
  rm -f "$FAIL_FILE"
fi

echo "Wrote output to $OUT_FILE" >&2
