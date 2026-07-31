#!/usr/bin/env python3
"""
Resolve outbound campaign queues, active queue members, and whole-agent counts.

Whole agent (definition A): an agent who is an *active* member of this campaign's
queue(s) and is not an active member of any other configured outbound campaign
queue. Genesys treats agents logged into / active on multiple campaigns as partial
agents on each; this script approximates that using non-inactive queue membership
across the campaign registry.

Active queue member: queue member row where inactive is not true. Optionally
include joined=true members only (--require-joined) for a point-in-time snapshot.

Requires:
  accessToken   OAuth bearer token (env var)
  GC_REGION     e.g. usw2.pure.cloud (default)

Writes:
  {dataDir}/campaign_whole_agents/whole_agents_summary.csv
  {dataDir}/campaign_whole_agents/whole_agents_summary.json
  {dataDir}/campaign_whole_agents/{campaign_id}.json

Usage:
  accessToken=... python3 scripts/fetch_campaign_whole_agents.py --all
  accessToken=... python3 scripts/fetch_campaign_whole_agents.py --campaign HOME_FA_OUTBOUND
  accessToken=... python3 scripts/fetch_campaign_whole_agents.py --all --require-joined
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from project_paths import DEFAULT_CONFIG, OUTBOUND_CSV_NAME, load_config, resolve_data_dir


def api_host() -> str:
    if os.environ.get("GC_API_HOST"):
        return os.environ["GC_API_HOST"]
    region = os.environ.get("GC_REGION") or os.environ.get("region", "usw2.pure.cloud")
    return f"api.{region}"


def api_get(url: str, token: str, retries: int = 5) -> Any:
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
            with urllib.request.urlopen(
                req, context=ssl.create_default_context(), timeout=90
            ) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            if exc.code == 401:
                raise RuntimeError(f"HTTP 401 unauthorized: {body}") from exc
            if exc.code in (429, 500, 502, 503, 504) and attempt < retries:
                print(
                    f"WARN: HTTP {exc.code}; retry in {backoff:.1f}s ({attempt}/{retries})",
                    file=sys.stderr,
                )
                time.sleep(backoff)
                backoff *= 2
                last_error = exc
                continue
            raise RuntimeError(f"HTTP {exc.code} for {url}: {body}") from exc
        except urllib.error.URLError as exc:
            if attempt < retries:
                print(
                    f"WARN: network error; retry in {backoff:.1f}s ({attempt}/{retries}): {exc}",
                    file=sys.stderr,
                )
                time.sleep(backoff)
                backoff *= 2
                last_error = exc
                continue
            raise
    if last_error:
        raise last_error
    raise RuntimeError("request failed")


def paginate_entities(host: str, path: str, token: str, page_size: int = 100) -> List[Dict[str, Any]]:
    entities: List[Dict[str, Any]] = []
    page_number = 1
    while True:
        query = urllib.parse.urlencode({"pageSize": page_size, "pageNumber": page_number})
        payload = api_get(f"https://{host}{path}?{query}", token)
        if not isinstance(payload, dict):
            break
        batch = payload.get("entities") or []
        if not isinstance(batch, list):
            break
        entities.extend(batch)
        page_count = payload.get("pageCount")
        if page_count is not None and page_number >= int(page_count):
            break
        if len(batch) < page_size:
            break
        page_number += 1
    return entities


def load_guid_map(data_dir: Path) -> Dict[str, str]:
    csv_path = data_dir / OUTBOUND_CSV_NAME
    if not csv_path.is_file():
        raise FileNotFoundError(f"Missing {csv_path}")
    guid_map: Dict[str, str] = {}
    with csv_path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            name = (row.get("name") or "").replace("\r", "").strip()
            guid = (row.get("guid") or "").replace("\r", "").strip()
            if name and guid:
                guid_map[name] = guid
    return guid_map


def member_type(member: Dict[str, Any]) -> Optional[str]:
    raw = member.get("type") or member.get("memberType")
    if isinstance(raw, str):
        return raw.lower()
    member_by = member.get("memberBy")
    if isinstance(member_by, str):
        return member_by.lower()
    if isinstance(member_by, dict):
        obj_type = member_by.get("objectType") or member_by.get("type")
        if isinstance(obj_type, str):
            return obj_type.lower()
    if member.get("user"):
        return "user"
    if member.get("group"):
        return "group"
    return None


def member_id(member: Dict[str, Any]) -> Optional[str]:
    if member.get("id"):
        return str(member["id"])
    member_by = member.get("memberBy")
    if isinstance(member_by, dict) and member_by.get("id"):
        return str(member_by["id"])
    nested = member.get("user") or member.get("group")
    if isinstance(nested, dict) and nested.get("id"):
        return str(nested["id"])
    return None


def is_active_member(member: Dict[str, Any], require_joined: bool) -> bool:
    if member.get("inactive") is True:
        return False
    if require_joined and member.get("joined") is not True:
        return False
    return True


def group_user_ids(
    host: str,
    group_id: str,
    token: str,
    cache: Dict[str, Set[str]],
    missing_groups: Optional[Set[str]] = None,
) -> Set[str]:
    if group_id in cache:
        return cache[group_id]
    users: Set[str] = set()
    try:
        for entity in paginate_entities(host, f"/api/v2/groups/{group_id}/members", token):
            if not isinstance(entity, dict):
                continue
            if str(entity.get("type", "")).lower() == "user" and entity.get("id"):
                users.add(str(entity["id"]))
    except RuntimeError as exc:
        if "HTTP 404" in str(exc):
            print(f"WARN: group {group_id} not found; skipping stale queue member", file=sys.stderr)
            if missing_groups is not None:
                missing_groups.add(group_id)
        else:
            raise
    cache[group_id] = users
    return users


def queue_active_user_ids(
    host: str,
    queue_id: str,
    token: str,
    require_joined: bool,
    group_cache: Dict[str, Set[str]],
) -> Tuple[Set[str], Dict[str, int]]:
    """Return active user IDs on a queue and member stats for reporting."""
    stats = {
        "members_total": 0,
        "members_inactive": 0,
        "members_not_joined": 0,
        "members_user": 0,
        "members_group": 0,
        "members_group_not_found": 0,
        "members_other": 0,
    }
    missing_groups: Set[str] = set()
    users: Set[str] = set()
    for member in paginate_entities(host, f"/api/v2/routing/queues/{queue_id}/members", token):
        if not isinstance(member, dict):
            continue
        stats["members_total"] += 1
        if member.get("inactive") is True:
            stats["members_inactive"] += 1
            continue
        if require_joined and member.get("joined") is not True:
            stats["members_not_joined"] += 1
            continue
        if not is_active_member(member, require_joined):
            continue

        mtype = member_type(member) or ""
        mid = member_id(member)
        if not mid:
            stats["members_other"] += 1
            continue
        if "user" in mtype:
            stats["members_user"] += 1
            users.add(mid)
        elif "group" in mtype:
            stats["members_group"] += 1
            before = len(missing_groups)
            users.update(group_user_ids(host, mid, token, group_cache, missing_groups))
            if len(missing_groups) > before:
                stats["members_group_not_found"] += 1
        else:
            stats["members_other"] += 1
    return users, stats


def fetch_campaign(host: str, guid: str, token: str) -> Dict[str, Any]:
    payload = api_get(f"https://{host}/api/v2/outbound/campaigns/{guid}", token)
    if not isinstance(payload, dict):
        raise RuntimeError(f"Unexpected campaign payload for {guid}")
    return payload


def fetch_queue(host: str, queue_id: str, token: str) -> Dict[str, Any]:
    payload = api_get(f"https://{host}/api/v2/routing/queues/{queue_id}", token)
    if not isinstance(payload, dict):
        raise RuntimeError(f"Unexpected queue payload for {queue_id}")
    return payload


def fetch_users(host: str, user_ids: Iterable[str], token: str) -> Dict[str, Dict[str, str]]:
    ids = sorted(set(user_ids))
    if not ids:
        return {}
    details: Dict[str, Dict[str, str]] = {}
    chunk_size = 50
    for start in range(0, len(ids), chunk_size):
        chunk = ids[start : start + chunk_size]
        query = urllib.parse.urlencode([("id", uid) for uid in chunk])
        payload = api_get(f"https://{host}/api/v2/users?{query}", token)
        if isinstance(payload, dict):
            for entity in payload.get("entities") or []:
                if isinstance(entity, dict) and entity.get("id"):
                    details[str(entity["id"])] = {
                        "name": str(entity.get("name") or ""),
                        "email": str(entity.get("email") or ""),
                    }
    return details


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Fetch whole-agent counts per outbound campaign.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--campaign", action="append", help="Campaign id (repeatable)")
    parser.add_argument("--all", action="store_true", help="All campaigns in campaigns.json")
    parser.add_argument("--skip-missing", action="store_true", help="Skip campaigns without guid")
    parser.add_argument(
        "--require-joined",
        action="store_true",
        help="Count only queue members with joined=true (runtime snapshot)",
    )
    parser.add_argument("--pause-ms", type=int, default=200, help="Pause between API calls")
    args = parser.parse_args(argv)

    token = os.environ.get("accessToken") or os.environ.get("ACCESS_TOKEN")
    if not token:
        print("ERROR: set accessToken environment variable", file=sys.stderr)
        return 1

    config = load_config(args.config)
    data_dir = resolve_data_dir(config)
    guid_map = load_guid_map(data_dir)
    campaigns = config.get("campaigns", [])

    if args.all:
        selected = [item["id"] for item in campaigns]
    elif args.campaign:
        selected = args.campaign
    else:
        selected = [item["id"] for item in campaigns]

    host = api_host()
    out_dir = data_dir / "campaign_whole_agents"
    out_dir.mkdir(parents=True, exist_ok=True)
    fetched_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    campaign_meta: Dict[str, Dict[str, Any]] = {}
    queue_users: Dict[str, Set[str]] = {}
    queue_stats: Dict[str, Dict[str, int]] = {}
    group_cache: Dict[str, Set[str]] = {}

    for campaign_id in selected:
        guid = guid_map.get(campaign_id)
        if not guid:
            if args.skip_missing:
                print(f"SKIP: {campaign_id} — no guid in {OUTBOUND_CSV_NAME}")
                continue
            print(f"ERROR: no guid for campaign {campaign_id}", file=sys.stderr)
            return 1

        print(f"Fetching campaign {campaign_id} ({guid})")
        campaign = fetch_campaign(host, guid, token)
        queue = campaign.get("queue") or {}
        queue_id = queue.get("id") if isinstance(queue, dict) else campaign.get("queueId")
        if not queue_id:
            print(f"WARN: {campaign_id} has no queueId", file=sys.stderr)
            campaign_meta[campaign_id] = {
                "campaign_id": campaign_id,
                "genesys_guid": guid,
                "campaign_name": campaign.get("name"),
                "queue_id": None,
                "queue_name": None,
                "error": "missing queueId",
            }
            continue

        if args.pause_ms > 0:
            time.sleep(args.pause_ms / 1000.0)

        queue_detail = fetch_queue(host, str(queue_id), token)
        users, stats = queue_active_user_ids(
            host, str(queue_id), token, args.require_joined, group_cache
        )
        queue_users[campaign_id] = users
        queue_stats[campaign_id] = stats
        campaign_meta[campaign_id] = {
            "campaign_id": campaign_id,
            "genesys_guid": guid,
            "campaign_name": campaign.get("name") or campaign_id,
            "queue_id": str(queue_id),
            "queue_name": queue_detail.get("name") or queue.get("name"),
            "dialing_mode": campaign.get("dialingMode"),
            "campaign_status": (campaign.get("campaignStatus") or {}).get("campaignStatus")
            if isinstance(campaign.get("campaignStatus"), dict)
            else campaign.get("campaignStatus"),
        }
        if args.pause_ms > 0:
            time.sleep(args.pause_ms / 1000.0)

    user_campaigns: Dict[str, Set[str]] = defaultdict(set)
    for campaign_id, users in queue_users.items():
        for user_id in users:
            user_campaigns[user_id].add(campaign_id)

    all_user_ids: Set[str] = set()
    for users in queue_users.values():
        all_user_ids.update(users)
    user_details = fetch_users(host, all_user_ids, token)

    summary_rows: List[Dict[str, Any]] = []
    for campaign_id, meta in sorted(campaign_meta.items()):
        users = queue_users.get(campaign_id, set())
        whole_ids = sorted(uid for uid in users if user_campaigns.get(uid) == {campaign_id})
        shared_ids = sorted(uid for uid in users if len(user_campaigns.get(uid, set())) > 1)
        whole_agents = []
        for uid in whole_ids:
            info = user_details.get(uid, {})
            whole_agents.append(
                {
                    "user_id": uid,
                    "name": info.get("name") or uid,
                    "email": info.get("email") or "",
                    "campaign_count": 1,
                }
            )
        shared_agents = []
        for uid in shared_ids:
            info = user_details.get(uid, {})
            shared_agents.append(
                {
                    "user_id": uid,
                    "name": info.get("name") or uid,
                    "email": info.get("email") or "",
                    "campaign_count": len(user_campaigns.get(uid, set())),
                    "campaigns": sorted(user_campaigns.get(uid, set())),
                }
            )

        record = {
            "fetched_at": fetched_at,
            "require_joined": args.require_joined,
            **meta,
            "queue_member_stats": queue_stats.get(campaign_id, {}),
            "active_queue_users": len(users),
            "whole_agent_count": len(whole_ids),
            "shared_agent_count": len(shared_ids),
            "whole_agents": whole_agents,
            "shared_agents": shared_agents,
        }
        summary_rows.append(
            {
                "campaign_id": campaign_id,
                "campaign_name": meta.get("campaign_name"),
                "queue_name": meta.get("queue_name"),
                "queue_id": meta.get("queue_id"),
                "active_queue_users": len(users),
                "whole_agent_count": len(whole_ids),
                "shared_agent_count": len(shared_ids),
                "members_inactive_excluded": queue_stats.get(campaign_id, {}).get("members_inactive", 0),
                "members_not_joined_excluded": queue_stats.get(campaign_id, {}).get("members_not_joined", 0),
            }
        )
        detail_path = out_dir / f"{campaign_id}.json"
        detail_path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        print(
            f"  {campaign_id}: queue={meta.get('queue_name')} "
            f"active={len(users)} whole={len(whole_ids)} shared={len(shared_ids)}"
        )

    csv_path = out_dir / "whole_agents_summary.csv"
    json_path = out_dir / "whole_agents_summary.json"
    fieldnames = [
        "campaign_id",
        "campaign_name",
        "queue_name",
        "queue_id",
        "active_queue_users",
        "whole_agent_count",
        "shared_agent_count",
        "members_inactive_excluded",
        "members_not_joined_excluded",
    ]
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(summary_rows)

    json_path.write_text(
        json.dumps(
            {
                "fetched_at": fetched_at,
                "require_joined": args.require_joined,
                "definition": (
                    "Whole agent = active (non-inactive) queue member on this campaign's queue "
                    "who is not an active member of any other configured campaign queue."
                ),
                "campaigns": summary_rows,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"Done -> {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
