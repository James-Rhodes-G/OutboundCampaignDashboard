#!/usr/bin/env python3
"""
Fetch Genesys outbound campaign diagnostic summaries.

GET /api/v2/outbound/diagnostics/campaigns/{campaignId}/summary

Requires:
  accessToken   OAuth bearer token (env var)
  GC_REGION     e.g. usw2.pure.cloud  (default: usw2.pure.cloud)
  GC_API_HOST   optional override, e.g. api.usw2.pure.cloud

Writes:
  {dataDir}/campaign_diagnostics/{campaign_id}.json
  {dataDir}/campaign_diagnostics/all_summaries.json

Usage:
  accessToken=... python3 scripts/fetch_campaign_diagnostics.py
  accessToken=... python3 scripts/fetch_campaign_diagnostics.py --campaign AUTO_FA_OUTBOUND
"""

from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from project_paths import DEFAULT_CONFIG, OUTBOUND_CSV_NAME, load_config, resolve_data_dir


DEFAULT_START = "2026-06-01T00:00:00.000Z"
DEFAULT_END = "2026-07-01T00:00:00.000Z"


def parse_time(value: str) -> datetime:
    text = value.strip()
    if text.isdigit():
        digits = int(text)
        if digits > 10_000_000_000:
            return datetime.fromtimestamp(digits / 1000, tz=timezone.utc)
        return datetime.fromtimestamp(digits, tz=timezone.utc)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return datetime.fromisoformat(text).astimezone(timezone.utc)


def to_epoch_ms(value: str) -> str:
    return str(int(parse_time(value).timestamp() * 1000))


def resolve_range(
    config: Dict[str, Any],
    start_arg: Optional[str],
    end_arg: Optional[str],
) -> Tuple[str, str, str, str]:
    diag = config.get("diagnostics") or {}
    start_label = (
        start_arg
        or os.environ.get("DIAGNOSTICS_START")
        or diag.get("start")
        or DEFAULT_START
    )
    end_label = (
        end_arg
        or os.environ.get("DIAGNOSTICS_END")
        or diag.get("end")
        or DEFAULT_END
    )
    return start_label, end_label, to_epoch_ms(start_label), to_epoch_ms(end_label)


def summary_url(host: str, guid: str, start_ms: str, end_ms: str) -> str:
    query = urllib.parse.urlencode({"start": start_ms, "end": end_ms})
    return (
        f"https://{host}/api/v2/outbound/diagnostics/campaigns/{guid}/summary?{query}"
    )


def api_host() -> str:
    if os.environ.get("GC_API_HOST"):
        return os.environ["GC_API_HOST"]
    region = os.environ.get("GC_REGION", "usw2.pure.cloud")
    return f"api.{region}"


def api_get(url: str, token: str, retries: int = 5) -> Dict[str, Any]:
    backoff = 1.0
    last_error: Optional[Exception] = None
    for attempt in range(1, retries + 1):
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, context=ssl.create_default_context(), timeout=90) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            if exc.code == 401:
                raise RuntimeError(f"HTTP 401 unauthorized: {body}") from exc
            if exc.code in (429, 500, 502, 503, 504) and attempt < retries:
                print(f"WARN: HTTP {exc.code}; retry in {backoff:.1f}s ({attempt}/{retries})", file=sys.stderr)
                time.sleep(backoff)
                backoff *= 2
                last_error = exc
                continue
            raise RuntimeError(f"HTTP {exc.code} for {url}: {body}") from exc
        except urllib.error.URLError as exc:
            if attempt < retries:
                print(f"WARN: network error; retry in {backoff:.1f}s ({attempt}/{retries}): {exc}", file=sys.stderr)
                time.sleep(backoff)
                backoff *= 2
                last_error = exc
                continue
            raise
    if last_error:
        raise last_error
    raise RuntimeError("request failed")


def load_guid_map(data_dir: Path) -> Dict[str, str]:
    csv_path = data_dir / OUTBOUND_CSV_NAME
    if not csv_path.is_file():
        raise FileNotFoundError(f"Missing {csv_path}")
    import csv

    guid_map: Dict[str, str] = {}
    with csv_path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            name = (row.get("name") or "").replace("\r", "").strip()
            guid = (row.get("guid") or "").replace("\r", "").strip()
            if name and guid:
                guid_map[name] = guid
    return guid_map


def pick_string(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        return text or None
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, dict):
        for key in ("state", "name", "status", "healthState", "value", "label"):
            if key in value:
                picked = pick_string(value[key])
                if picked:
                    return picked
        return json.dumps(value, sort_keys=True)
    if isinstance(value, list):
        if not value:
            return None
        return json.dumps(value, sort_keys=True)
    return str(value)


def extract_summary_fields(payload: Dict[str, Any]) -> Dict[str, Optional[str]]:
    health = pick_string(
        payload.get("healthState")
        or payload.get("healthStatus")
        or payload.get("campaignHealthState")
        or payload.get("overallHealthState")
        or payload.get("currentHealthState")
    )
    campaign_state = pick_string(
        payload.get("campaignState")
        or payload.get("state")
        or payload.get("operationalState")
    )
    if not campaign_state:
        states = payload.get("campaignStates")
        if isinstance(states, list) and states:
            latest = states[-1]
            if isinstance(latest, dict):
                campaign_state = pick_string(latest.get("state"))
    if not health:
        health_states = payload.get("campaignHealthStates")
        if isinstance(health_states, list) and health_states:
            latest_health = health_states[-1]
            if isinstance(latest_health, dict):
                health = pick_string(latest_health.get("state"))
    return {
        "health_state": health,
        "campaign_state": campaign_state,
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Fetch Genesys campaign diagnostic summaries.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--campaign", action="append", help="Campaign id (repeatable)")
    parser.add_argument("--all", action="store_true", help="Fetch all configured campaigns")
    parser.add_argument("--skip-missing", action="store_true", help="Skip campaigns without a guid")
    parser.add_argument("--pause-ms", type=int, default=250, help="Pause between API calls")
    parser.add_argument("--start", help="Interval start (ISO 8601, default June 2026)")
    parser.add_argument("--end", help="Interval end (ISO 8601, default July 2026)")
    args = parser.parse_args(argv)

    token = os.environ.get("accessToken") or os.environ.get("ACCESS_TOKEN")
    if not token:
        print("ERROR: set accessToken environment variable", file=sys.stderr)
        return 1

    config = load_config(args.config)
    data_dir = resolve_data_dir(config)
    guid_map = load_guid_map(data_dir)
    campaigns = config.get("campaigns", [])
    start, end, start_ms, end_ms = resolve_range(config, args.start, args.end)

    if args.all:
        selected = [item["id"] for item in campaigns]
    elif args.campaign:
        selected = args.campaign
    else:
        selected = [item["id"] for item in campaigns]

    out_dir = data_dir / "campaign_diagnostics"
    out_dir.mkdir(parents=True, exist_ok=True)
    fetched_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    host = api_host()
    print(f"Interval: {start} -> {end} ({start_ms} .. {end_ms})")
    summaries: List[Dict[str, Any]] = []
    ok = 0
    failed = 0

    for campaign_id in selected:
        guid = guid_map.get(campaign_id)
        if not guid:
            if args.skip_missing:
                print(f"SKIP: {campaign_id} — no guid in {OUTBOUND_CSV_NAME}")
                continue
            print(f"ERROR: no guid for campaign {campaign_id}", file=sys.stderr)
            return 1

        url = summary_url(host, guid, start_ms, end_ms)
        print(f"Fetching {campaign_id} ({guid})")
        try:
            payload = api_get(url, token)
        except Exception as exc:
            failed += 1
            print(f"ERROR: {campaign_id}: {exc}", file=sys.stderr)
            continue

        extracted = extract_summary_fields(payload if isinstance(payload, dict) else {})
        record = {
            "campaign_id": campaign_id,
            "genesys_guid": guid,
            "fetched_at": fetched_at,
            "interval_start": start,
            "interval_end": end,
            "health_state": extracted["health_state"],
            "campaign_state": extracted["campaign_state"],
            "summary": payload,
        }
        out_path = out_dir / f"{campaign_id}.json"
        out_path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        summaries.append(record)
        ok += 1
        if args.pause_ms > 0:
            time.sleep(args.pause_ms / 1000.0)

    aggregate_path = out_dir / "all_summaries.json"
    aggregate_path.write_text(json.dumps(summaries, indent=2) + "\n", encoding="utf-8")
    print(f"Done. fetched={ok} failed={failed} -> {out_dir}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
